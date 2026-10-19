from decimal import Decimal

from rest_framework import serializers
from django.db import transaction

from common.validators import reject_branch_mismatch, require_bilingual_pair
from inventory.models import InventoryItem, StockMovement

from .models import Purchase, PurchaseItem, PurchaseStatus, Supplier, SupplierDocument, SupplierDocumentKind
from .vat import check_qr


class SupplierSerializer(serializers.ModelSerializer):
    # Neither language is unconditionally required — see require_bilingual_pair
    # in validate(), which only requires whichever one the admin UI has active.
    name_en = serializers.CharField(required=False, allow_blank=True)
    name_ar = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Supplier
        fields = ["id", "name_en", "name_ar", "contact_info", "vat_registered", "vat_number", "crn", "address",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        from zatca.validators import other_id_error, vat_number_error

        require_bilingual_pair(attrs, self.instance, "name")
        errors = {}
        vat_number = attrs.get("vat_number", "").strip() if "vat_number" in attrs else None
        if vat_number:
            code = vat_number_error(vat_number)
            if code:
                errors["vat_number"] = code
            attrs["vat_number"] = vat_number
        crn = attrs.get("crn", "").strip() if "crn" in attrs else None
        if crn:
            code = other_id_error(crn, "CRN")
            if code:
                errors["crn"] = code
            attrs["crn"] = crn
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class SupplierDocumentSerializer(serializers.ModelSerializer):
    """The supplier's tax invoice or credit note, as keyed in from the
    paper/PDF. The QR is checked on every save (purchasing/vat.py)."""

    amount_messages = {"min_value": "error.amountInvalid", "invalid": "error.amountInvalid",
                       "required": "error.amountInvalid", "max_digits": "error.amountInvalid",
                       "max_decimal_places": "error.amountInvalid", "null": "error.amountInvalid"}
    net = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, error_messages=amount_messages)
    vat = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, error_messages=amount_messages)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, error_messages=amount_messages)
    number = serializers.CharField(max_length=64, error_messages={"blank": "error.supplierInvoiceNumberRequired",
                                                                  "required": "error.supplierInvoiceNumberRequired"})
    issue_date = serializers.DateField(error_messages={"invalid": "error.dateInvalid", "required": "error.dateInvalid",
                                                       "null": "error.dateInvalid"})

    class Meta:
        model = SupplierDocument
        fields = ["id", "purchase", "kind", "number", "issue_date", "net", "vat", "total", "reason", "qr_text",
                  "qr_check", "xml", "pdf_url", "created_at", "updated_at"]
        read_only_fields = ["id", "purchase", "kind", "qr_check", "created_at", "updated_at"]
        extra_kwargs = {"reason": {"required": False}, "qr_text": {"required": False},
                        "xml": {"required": False}, "pdf_url": {"required": False}}

    def validate(self, attrs):
        purchase = self.context["purchase"]
        supplier = purchase.supplier
        merged = {name: attrs.get(name, getattr(self.instance, name, None)) for name in ("net", "vat", "total")}
        errors = {}
        if not supplier.vat_registered and merged["vat"]:
            # A supplier outside VAT can't charge it; nothing to claim either.
            errors["vat"] = "error.supplierNotVatRegistered"
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def save(self, **kwargs):
        purchase = self.context["purchase"]
        saved = ({name: getattr(self.instance, name) for name in ("qr_text", "issue_date", "total", "vat")}
                 if self.instance else {})
        values = {**saved, **self.validated_data}
        kwargs["qr_check"] = check_qr(values.get("qr_text", ""), supplier_vat=purchase.supplier.vat_number,
                                      issue_date=values.get("issue_date"), total=values.get("total"),
                                      vat=values.get("vat"))
        return super().save(**kwargs)


class PurchaseItemSerializer(serializers.ModelSerializer):
    inventory_name = serializers.CharField(source="inventory_item.name", read_only=True)
    unit = serializers.CharField(source="inventory_item.unit", read_only=True)
    line_total = serializers.SerializerMethodField()
    received_quantity = serializers.SerializerMethodField()
    stock_before = serializers.SerializerMethodField()
    stock_added = serializers.SerializerMethodField()
    stock_after = serializers.SerializerMethodField()
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"),
        error_messages={"min_value": "error.quantityInvalid", "required": "error.quantityInvalid"},
    )
    unit_cost = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0,
        error_messages={"min_value": "error.unitCostInvalid", "required": "error.unitCostInvalid"},
    )

    class Meta:
        model = PurchaseItem
        fields = [
            "id", "inventory_item", "inventory_name", "unit", "quantity", "received_quantity",
            "unit_cost", "line_total", "stock_before", "stock_added", "stock_after",
        ]

    def _movement(self, obj):
        if obj.purchase.status != PurchaseStatus.RECEIVED:
            return None
        if hasattr(obj, "movements_by_id"):  # prefetched by PurchaseViewSet
            return obj.movements_by_id[0] if obj.movements_by_id else None
        return StockMovement.objects.filter(
            purchase_item_id=obj.pk,
        ).order_by("id").first()

    def get_line_total(self, obj):
        return obj.quantity * obj.unit_cost

    def get_received_quantity(self, obj):
        return obj.quantity if obj.purchase.status == PurchaseStatus.RECEIVED else None

    def get_stock_before(self, obj):
        movement = self._movement(obj)
        return movement.previous_stock if movement else None

    def get_stock_added(self, obj):
        movement = self._movement(obj)
        return movement.quantity_delta if movement else None

    def get_stock_after(self, obj):
        movement = self._movement(obj)
        return movement.resulting_stock if movement else None


class PurchaseSerializer(serializers.ModelSerializer):
    items = PurchaseItemSerializer(many=True)
    supplier_name = serializers.CharField(source="supplier.name_en", read_only=True)
    branch_name = serializers.CharField(source="branch.name_en", read_only=True)
    currency = serializers.CharField(source="branch.currency", read_only=True)
    subtotal = serializers.SerializerMethodField()
    supplier_invoice = serializers.SerializerMethodField()
    supplier_credit_notes = serializers.SerializerMethodField()

    class Meta:
        model = Purchase
        fields = ["id", "branch", "branch_name", "currency", "supplier", "supplier_name", "status", "items", "subtotal",
                  "supplier_invoice", "supplier_credit_notes", "ordered_at", "received_at", "created_at", "updated_at"]
        read_only_fields = ["id", "ordered_at", "received_at", "created_at", "updated_at"]

    def get_subtotal(self, obj):
        return sum((item.quantity * item.unit_cost for item in obj.items.all()), Decimal("0"))

    def _documents(self, obj, kind):
        # .all() so PurchaseViewSet's prefetch serves every row from memory.
        return [document for document in obj.supplier_documents.all() if document.kind == kind]

    def get_supplier_invoice(self, obj):
        documents = self._documents(obj, SupplierDocumentKind.INVOICE)
        return SupplierDocumentSerializer(documents[0], context={"purchase": obj}).data if documents else None

    def get_supplier_credit_notes(self, obj):
        return SupplierDocumentSerializer(self._documents(obj, SupplierDocumentKind.CREDIT_NOTE), many=True,
                                          context={"purchase": obj}).data

    def validate_status(self, value):
        # Only POST /api/purchases/{id}/receive/ may set this — going
        # through a plain PATCH would skip the stock deduction + cost update
        # that receiving is supposed to trigger.
        if value == PurchaseStatus.RECEIVED:
            raise serializers.ValidationError("error.usePurchaseReceiveEndpoint")
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("error.purchaseItemsRequired")
        return value

    def validate(self, attrs):
        branch = attrs.get("branch") or getattr(self.instance, "branch", None)
        reject_branch_mismatch(self.context.get("request"), branch)

        items = attrs.get("items")
        if items:
            inventory_item_ids = {item["inventory_item"].id for item in items}
            valid_count = InventoryItem.objects.filter(pk__in=inventory_item_ids, branch=branch).count()
            if valid_count != len(inventory_item_ids):
                raise serializers.ValidationError({"items": "error.inventoryItemInvalid"})
        return attrs

    def create(self, validated_data):
        items_data = validated_data.pop("items")
        with transaction.atomic():
            purchase = Purchase.objects.create(**validated_data)
            PurchaseItem.objects.bulk_create([PurchaseItem(purchase=purchase, **item) for item in items_data])
        return purchase

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)
        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if items_data is not None:
                instance.items.all().delete()
                PurchaseItem.objects.bulk_create([PurchaseItem(purchase=instance, **item) for item in items_data])
        return instance
