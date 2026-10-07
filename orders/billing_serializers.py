"""
Read serializers for Bill, and the small input serializers for the billing
endpoints.

Nothing here accepts an amount. The only money that crosses this boundary goes
outward, computed by orders/billing.py from the database.
"""

from rest_framework import serializers

from .billing import (
    amount_outstanding,
    amount_paid,
    bill_total,
    is_ready_to_pay,
    order_total,
    to_halalas,
    unserved_orders,
)
from .models import Bill, BillMethod, MoyasarPayment


class BillOrderSerializer(serializers.Serializer):
    """An order as it appears on a bill — enough to itemise a receipt."""

    id = serializers.IntegerField()
    order_code = serializers.CharField()
    status = serializers.CharField()
    opened_at = serializers.DateTimeField()
    items = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    def get_items(self, order):
        return [
            {
                "id": item.id,
                "name_en": item.name_en_snapshot,
                "name_ar": item.name_ar_snapshot,
                # The portion as ordered. Snapshotted, so a later rename never
                # changes what an already-printed bill says.
                "variant_name_en": item.variant_name_en_snapshot,
                "variant_name_ar": item.variant_name_ar_snapshot,
                "price": str(item.price_snapshot),
                "quantity": item.quantity,
                "status": item.status,
                "notes": item.notes,
            }
            for item in order.items.all()
        ]

    def get_total(self, order):
        return str(order_total(order))


class BillSerializer(serializers.ModelSerializer):
    """
    The shape every billing endpoint returns.

    `total` and `total_halalas` are recomputed from current bill lines. The
    payment request amount (`total_halalas`) is the outstanding balance, so
    an already-paid order is never charged twice after more items are added.
    """

    table_label = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    total_halalas = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    orders = serializers.SerializerMethodField()
    ready_to_pay = serializers.SerializerMethodField()
    unserved_order_ids = serializers.SerializerMethodField()
    needs_review = serializers.SerializerMethodField()
    # What has already been taken, and what is still owed. These differ from
    # `total` only when staff added items to an already-paid bill before the
    # table was released — the guest then pays the difference, not the total.
    amount_paid = serializers.SerializerMethodField()
    amount_outstanding = serializers.SerializerMethodField()

    class Meta:
        model = Bill
        fields = [
            "id", "bill_code", "branch", "table", "table_label", "status",
            "requested_method", "paid_method", "total", "total_halalas", "currency",
            "total_at_payment", "ready_to_pay", "unserved_order_ids", "needs_review",
            "amount_paid", "amount_outstanding",
            "orders", "pay_requested_at", "paid_at", "closed_at", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_table_label(self, bill):
        return bill.table.label_en if bill.table_id else None

    def get_total(self, bill):
        return str(bill_total(bill))

    def get_total_halalas(self, bill):
        return to_halalas(amount_outstanding(bill))

    def get_currency(self, bill):
        from django.conf import settings
        return settings.BILL_CURRENCY

    def get_orders(self, bill):
        return BillOrderSerializer(bill.orders.all(), many=True).data

    def get_ready_to_pay(self, bill):
        return is_ready_to_pay(bill)

    def get_unserved_order_ids(self, bill):
        return [order.pk for order in unserved_orders(bill)]

    def get_amount_paid(self, bill):
        return str(amount_paid(bill))

    def get_amount_outstanding(self, bill):
        return str(amount_outstanding(bill))

    def get_needs_review(self, bill):
        return any(payment.needs_review for payment in bill.moyasar_payments.all())


class MoyasarPaymentSerializer(serializers.ModelSerializer):
    """Staff-only. `raw` is deliberately excluded — it is an audit field for
    Django admin, not something a POS screen needs."""

    class Meta:
        model = MoyasarPayment
        fields = [
            "id", "payment_id", "status", "amount_halalas", "currency",
            "source_type", "source_company", "needs_review", "review_reason", "verified_at",
        ]
        read_only_fields = fields


class RequestPaySerializer(serializers.Serializer):
    session_token = serializers.CharField(error_messages={"required": "error.sessionTokenRequired"})
    method = serializers.ChoiceField(
        choices=[BillMethod.CASH, BillMethod.ONLINE],
        error_messages={"required": "error.paymentMethodRequired", "invalid_choice": "error.paymentMethodInvalid"},
    )


class CancelPaySerializer(serializers.Serializer):
    session_token = serializers.CharField(error_messages={"required": "error.sessionTokenRequired"})


class VerifyOnlineSerializer(serializers.Serializer):
    session_token = serializers.CharField(error_messages={"required": "error.sessionTokenRequired"})
    payment_id = serializers.CharField(
        max_length=64, error_messages={"required": "error.paymentIdRequired"}
    )


class SettleBillSerializer(serializers.Serializer):
    """Cashier taking payment at the till: cash in the drawer, or a card on
    the restaurant's own terminal. `online` is not selectable here — that
    status can only ever be reached by verifying a real Moyasar payment."""

    method = serializers.ChoiceField(
        choices=[BillMethod.CASH, BillMethod.CARD],
        default=BillMethod.CASH,
        error_messages={"invalid_choice": "error.paymentMethodInvalid"},
    )
