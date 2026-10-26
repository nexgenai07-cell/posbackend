from decimal import Decimal

from rest_framework import serializers

from catalog.models import Product, ProductVariant
from catalog.services import current_price, is_available_today
from common.exceptions import ConflictCode
from inventory.services import check_product_stock, variant_multiplier

from .models import Order, OrderHistory, OrderItem, OrderItemStatus, Payment

# History events whose details.item_ids mark lines added after the first batch.
ADDITIONAL_BATCH_EVENTS = (
    "customer_items_added", "customer_items_reviewed_and_sent",
    "cashier_item_added_and_sent", "waiter_item_added_and_sent",
)


def additional_batch_item_ids(order):
    """Ids of the order's lines that arrived as a later batch ("Added later")."""
    return {
        int(item_id)
        for entry in order.history.all()
        if entry.event in ADDITIONAL_BATCH_EVENTS
        for item_id in (entry.details or {}).get("item_ids", [])
        if str(item_id).isdigit()
    }


class OrderHistorySerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.order_number", read_only=True)

    class Meta:
        model = OrderHistory
        fields = ["id", "order", "order_number", "actor", "event", "from_status", "to_status", "details", "created_at"]
        read_only_fields = fields


class OrderItemSerializer(serializers.ModelSerializer):
    is_new = serializers.SerializerMethodField()
    is_additional_batch = serializers.SerializerMethodField()

    def get_is_new(self, item):
        return bool(getattr(item, "_is_additional_batch", False) and item.status not in (OrderItemStatus.SERVED, OrderItemStatus.VOIDED))

    def get_is_additional_batch(self, item):
        return bool(getattr(item, "_is_additional_batch_any", False))

    class Meta:
        model = OrderItem
        fields = [
            "id", "order", "product", "variant", "name_en_snapshot", "name_ar_snapshot",
            "variant_name_en_snapshot", "variant_name_ar_snapshot",
            "price_snapshot", "quantity", "status", "notes", "fired_at", "ready_at",
            "created_at", "updated_at", "is_new", "is_additional_batch",
        ]
        # Snapshot fields are write-once at creation (see AddOrderItemSerializer)
        # and never editable afterward — that's the whole point of a snapshot.
        # fired_at/ready_at are set only by SendToKitchenView/OrderItemStatusView.
        read_only_fields = [
            "id", "order", "product", "variant", "name_en_snapshot", "name_ar_snapshot",
            "variant_name_en_snapshot", "variant_name_ar_snapshot",
            "price_snapshot", "status", "fired_at", "ready_at", "created_at", "updated_at",
        ]


class AddOrderItemSerializer(serializers.Serializer):
    """
    Adds a line to an order. With context {"merge_pending": True} the same
    dish (product, portion, note and price) tapped again goes into the line
    already waiting to be sent — its quantity grows — instead of a second row.
    Only a PENDING line is ever merged into: once a line is in the kitchen its
    quantity is fixed, and the next one is a new kitchen ticket. After save(),
    `added_quantity` is how many this call added (the line may hold more).
    """

    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(),
        error_messages={"required": "error.productRequired", "does_not_exist": "error.productRequired"},
    )
    # Optional: products with no variants are ordered exactly as before.
    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), required=False, allow_null=True,
        error_messages={"does_not_exist": "error.variantInvalid"},
    )
    quantity = serializers.IntegerField(default=1, min_value=1, error_messages={"min_value": "error.quantityInvalid"})
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_product(self, product):
        order = self.context["order"]
        if product.branch_id != order.branch_id or not is_available_today(product):
            # 409, the same status dispatch uses for this code.
            raise ConflictCode("error.productUnavailable")
        return product

    def validate(self, attrs):
        product = attrs.get("product")
        quantity = attrs.get("quantity", 1)
        variant = attrs.get("variant")

        if product and variant is not None:
            # A variant id belonging to a DIFFERENT product would otherwise let
            # a client pay the Half KG price for a 2 KG tray. Same class of
            # cross-entity check as the bill/table one in billing_views.py.
            if variant.product_id != product.pk:
                raise serializers.ValidationError({"variant": "error.variantInvalid"})
            if not variant.is_active:
                raise serializers.ValidationError({"variant": "error.variantUnavailable"})

        self._merge_into = self._pending_line(product, variant, attrs.get("notes", "")) if product else None
        if product:
            # Stock is checked against the SELECTED portion: a 2 KG order needs
            # twice the ingredients, so the base recipe passing proves nothing.
            # A merged line is checked for its new total, not just the delta.
            total = quantity + (self._merge_into.quantity if self._merge_into else 0)
            available, _reason = check_product_stock(product, total, variant=variant)
            if not available:
                raise ConflictCode("error.productUnavailable")
        return attrs

    def _pending_line(self, product, variant, notes):
        if not self.context.get("merge_pending"):
            return None
        return (
            OrderItem.objects.select_for_update()
            .filter(
                order=self.context["order"], product=product, variant=variant,
                status=OrderItemStatus.PENDING, notes=(notes or "").strip(),
                price_snapshot=current_price(product, variant),
            )
            .order_by("id")
            .first()
        )

    def create(self, validated_data):
        order = self.context["order"]
        product = validated_data["product"]
        variant = validated_data.get("variant")
        self.added_quantity = validated_data["quantity"]
        line = getattr(self, "_merge_into", None)
        if line is not None:
            line.quantity += validated_data["quantity"]
            line.save(update_fields=["quantity", "updated_at"])
            return line
        return OrderItem.objects.create(
            order=order,
            product=product,
            variant=variant,
            name_en_snapshot=product.name_en,
            name_ar_snapshot=product.name_ar,
            # Frozen at order time. Renaming a variant later must never rewrite
            # what an old receipt says it was.
            variant_name_en_snapshot=variant.name_en if variant else "",
            variant_name_ar_snapshot=variant.name_ar if variant else "",
            # current_price(), not product.price directly — a product with an
            # active deal must snapshot the deal price, the same one the menu
            # just showed, not the full price (see catalog/services.py). With a
            # variant this resolves the variant's price, discounted if a deal
            # window is open.
            price_snapshot=current_price(product, variant),
            quantity=validated_data["quantity"],
            notes=(validated_data.get("notes", "") or "").strip(),
        )


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ["id", "order", "amount", "method", "tip", "paid_at", "created_at", "updated_at"]
        read_only_fields = ["id", "order", "paid_at", "created_at", "updated_at"]


class RecordPaymentSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
        error_messages={"min_value": "error.amountInvalid", "required": "error.amountInvalid"},
    )

    class Meta:
        model = Payment
        fields = ["amount", "method", "tip"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    history = OrderHistorySerializer(many=True, read_only=True)
    total = serializers.SerializerMethodField()

    def to_representation(self, instance):
        new_ids = additional_batch_item_ids(instance)
        items = instance.items.all()
        for item in items:
            item._is_additional_batch_any = item.pk in new_ids
            item._is_additional_batch = item.pk in new_ids
        return super().to_representation(instance)

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "order_code", "branch", "table", "customer", "source", "staff",
            "assigned_cashier", "assigned_waiter", "waiter_confirmed_at", "waiter_confirmed_by",
            "status", "payment_status", "items", "payments", "history", "total",
            "opened_at", "closed_at", "cancelled_at", "cancel_reason", "cancelled_by", "cancelled_from_status",
            "cancelled_total", "refunded_amount", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_total(self, order):
        return sum(
            (item.price_snapshot * item.quantity for item in order.items.all() if item.status != OrderItemStatus.VOIDED),
            Decimal("0.00"),
        )


class KitchenTicketItemSerializer(serializers.ModelSerializer):
    """One line as the cook needs it: the portion ordered and what goes into it.

    `recipe` lists the product's CURRENT recipe in recipe units (g, ml, pcs —
    what the cook measures in), with the arithmetic spelled out rather than
    pre-multiplied: per_portion x recipe_multiplier x quantity = total. The
    multiplier is the variant's (1 KG = 2 x Half KG); 1 for unvarianted lines.
    """

    recipe_multiplier = serializers.SerializerMethodField()
    recipe = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "id", "product", "name_en_snapshot", "name_ar_snapshot",
            "variant_name_en_snapshot", "variant_name_ar_snapshot",
            "quantity", "status", "notes", "fired_at", "ready_at",
            "recipe_multiplier", "recipe",
        ]
        read_only_fields = fields

    def get_recipe_multiplier(self, item):
        return str(variant_multiplier(item.variant))

    def get_recipe(self, item):
        multiplier = variant_multiplier(item.variant)
        return [
            {
                "inventory_item": line.inventory_item_id,
                "name": line.inventory_item.name,
                "unit": line.unit,
                "per_portion": str(line.quantity),
                "total": str((line.quantity * multiplier * item.quantity).quantize(Decimal("0.001"))),
            }
            for line in item.product.recipe_items.all()
        ]


class KitchenTicketOrderSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    table_label_en = serializers.CharField(source="table.label_en", default=None, read_only=True)
    table_label_ar = serializers.CharField(source="table.label_ar", default=None, read_only=True)
    assigned_waiter_name = serializers.CharField(source="assigned_waiter.name", default=None, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "order_code", "source", "table", "table_label_en", "table_label_ar",
            "assigned_waiter", "assigned_waiter_name",
            "status", "items", "opened_at", "waiter_confirmed_at", "closed_at",
        ]
        read_only_fields = fields

    def get_items(self, order):
        # Filtered in Python over the prefetched lines so the recipe/variant
        # prefetch in KitchenTicketsView is actually used (a .filter() here
        # would re-query every order).
        lines = sorted(order.items.all(), key=lambda item: item.pk)
        if self.context.get("scope") == "history":
            # The kitchen log: everything that was ever fired, in any outcome.
            eligible = [item for item in lines if item.fired_at is not None]
            return KitchenTicketItemSerializer(eligible, many=True).data
        new_ids = additional_batch_item_ids(order)
        cooking = (OrderItemStatus.FIRED, OrderItemStatus.PREPARING)
        active_batch = [item for item in lines if item.status in cooking]
        additional_batch = [item for item in lines if item.pk in new_ids and item.status in (*cooking, OrderItemStatus.READY)]
        if additional_batch:
            eligible = additional_batch
        else:
            eligible = active_batch or [item for item in lines if item.status == OrderItemStatus.READY]
        return KitchenTicketItemSerializer(eligible, many=True).data


class PublicOrderItemSerializer(serializers.ModelSerializer):
    """Same fields as OrderItemSerializer minus `order`/`product` (internal
    FKs a customer's phone has no use for) — used by the public QR endpoints."""

    is_new = serializers.SerializerMethodField()
    is_additional_batch = serializers.SerializerMethodField()

    def get_is_new(self, item):
        return bool(getattr(item, "_is_additional_batch", False) and item.status not in (OrderItemStatus.SERVED, OrderItemStatus.VOIDED))

    def get_is_additional_batch(self, item):
        return bool(getattr(item, "_is_additional_batch_any", False))

    class Meta:
        model = OrderItem
        fields = [
            "id", "name_en_snapshot", "name_ar_snapshot", "variant_name_en_snapshot", "variant_name_ar_snapshot", "price_snapshot",
            "quantity", "status", "notes", "fired_at", "ready_at", "is_new", "is_additional_batch",
        ]
        read_only_fields = fields


class PublicOrderSerializer(serializers.ModelSerializer):
    """OrderSerializer without `branch`/`staff`/`customer` — nothing a public,
    unauthenticated client (a customer's phone) needs or should see.

    Voided lines are left out entirely (the total already excluded them), and
    the bill's state rides along so the customer screen can say "request
    payment again" straight from the order response."""

    items = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    bill_id = serializers.IntegerField(read_only=True, allow_null=True)
    bill_status = serializers.SerializerMethodField()
    payment_request_cancelled_by_add = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "order_code", "tracking_token", "table", "bill_id", "bill_status",
            "payment_request_cancelled_by_add", "source", "status", "items", "total", "opened_at", "closed_at",
        ]
        read_only_fields = fields

    def get_items(self, order):
        new_ids = additional_batch_item_ids(order)
        visible = [item for item in order.items.all() if item.status != OrderItemStatus.VOIDED]
        for item in visible:
            item._is_additional_batch_any = item.pk in new_ids
            item._is_additional_batch = item.pk in new_ids and item.status != OrderItemStatus.SERVED
        return PublicOrderItemSerializer(visible, many=True).data

    def get_bill_status(self, order):
        return order.bill.status if order.bill_id else None

    def get_payment_request_cancelled_by_add(self, order):
        if not order.bill_id:
            return False
        from .billing import payment_request_cancelled_by_add
        return payment_request_cancelled_by_add(order.bill)

    def get_total(self, order):
        return sum(
            (item.price_snapshot * item.quantity for item in order.items.all() if item.status != OrderItemStatus.VOIDED),
            Decimal("0.00"),
        )
