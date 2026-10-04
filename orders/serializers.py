from decimal import Decimal

from rest_framework import serializers

from catalog.models import Product
from catalog.services import current_price, is_available_today
from inventory.services import check_product_stock

from .models import Order, OrderHistory, OrderItem, OrderItemStatus, Payment


class OrderHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderHistory
        fields = ["id", "actor", "event", "from_status", "to_status", "details", "created_at"]
        read_only_fields = fields


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            "id", "order", "product", "name_en_snapshot", "name_ar_snapshot",
            "price_snapshot", "quantity", "status", "notes", "fired_at", "ready_at",
            "created_at", "updated_at",
        ]
        # Snapshot fields are write-once at creation (see AddOrderItemSerializer)
        # and never editable afterward — that's the whole point of a snapshot.
        # fired_at/ready_at are set only by SendToKitchenView/OrderItemStatusView.
        read_only_fields = [
            "id", "order", "product", "name_en_snapshot", "name_ar_snapshot",
            "price_snapshot", "status", "fired_at", "ready_at", "created_at", "updated_at",
        ]


class AddOrderItemSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(),
        error_messages={"required": "error.productRequired", "does_not_exist": "error.productRequired"},
    )
    quantity = serializers.IntegerField(default=1, min_value=1, error_messages={"min_value": "error.quantityInvalid"})
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_product(self, product):
        order = self.context["order"]
        if product.branch_id != order.branch_id or not is_available_today(product):
            raise serializers.ValidationError("error.productUnavailable")
        return product

    def validate(self, attrs):
        product = attrs.get("product")
        quantity = attrs.get("quantity", 1)
        if product:
            available, _reason = check_product_stock(product, quantity)
            if not available:
                raise serializers.ValidationError({"product": "error.productUnavailable"})
        return attrs

    def create(self, validated_data):
        order = self.context["order"]
        product = validated_data["product"]
        return OrderItem.objects.create(
            order=order,
            product=product,
            name_en_snapshot=product.name_en,
            name_ar_snapshot=product.name_ar,
            # current_price(), not product.price directly — a product with an
            # active deal must snapshot the deal price, the same one the menu
            # just showed, not the full price (see catalog/services.py).
            price_snapshot=current_price(product),
            quantity=validated_data["quantity"],
            notes=validated_data.get("notes", ""),
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

    class Meta:
        model = Order
        fields = [
            "id", "order_code", "branch", "table", "customer", "source", "staff",
            "assigned_cashier", "assigned_waiter", "waiter_confirmed_at", "waiter_confirmed_by",
            "status", "payment_status", "items", "payments", "history", "total",
            "opened_at", "closed_at", "cancelled_at", "cancel_reason", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_total(self, order):
        return sum(
            (item.price_snapshot * item.quantity for item in order.items.all() if item.status != OrderItemStatus.VOIDED),
            Decimal("0.00"),
        )


class KitchenTicketItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ["id", "name_en_snapshot", "name_ar_snapshot", "quantity", "status", "notes", "fired_at", "ready_at"]
        read_only_fields = fields


class KitchenTicketOrderSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "order_code", "table", "assigned_waiter", "status", "items", "opened_at"]
        read_only_fields = fields

    def get_items(self, order):
        eligible = order.items.filter(status__in=(OrderItemStatus.FIRED, OrderItemStatus.PREPARING, OrderItemStatus.READY))
        return KitchenTicketItemSerializer(eligible, many=True).data


class PublicOrderItemSerializer(serializers.ModelSerializer):
    """Same fields as OrderItemSerializer minus `order`/`product` (internal
    FKs a customer's phone has no use for) — used by the public QR endpoints."""

    class Meta:
        model = OrderItem
        fields = [
            "id", "name_en_snapshot", "name_ar_snapshot", "price_snapshot",
            "quantity", "status", "notes", "fired_at", "ready_at",
        ]
        read_only_fields = fields


class PublicOrderSerializer(serializers.ModelSerializer):
    """OrderSerializer without `branch`/`staff`/`customer` — nothing a public,
    unauthenticated client (a customer's phone) needs or should see."""

    items = PublicOrderItemSerializer(many=True, read_only=True)
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "order_code", "table", "source", "status", "items", "total", "opened_at", "closed_at"]
        read_only_fields = fields

    def get_total(self, order):
        return sum(
            (item.price_snapshot * item.quantity for item in order.items.all() if item.status != OrderItemStatus.VOIDED),
            Decimal("0.00"),
        )
