from django.db import models

from accounts.models import Staff
from branches.models import Branch
from catalog.models import Product
from common.models import BaseModel
from customers.models import Customer
from tables.models import Table


class OrderSource(models.TextChoices):
    POS = "pos", "POS"
    QR = "qr", "QR"
    WEB = "web", "Website"


class OrderStatus(models.TextChoices):
    OPEN = "open", "Open"
    PENDING_CASHIER = "pending_cashier", "Pending cashier review"
    AWAITING_WAITER = "awaiting_waiter", "Awaiting waiter confirmation"
    CONFIRMED = "confirmed", "Confirmed"
    SENT = "sent", "Sent"
    PREPARING = "preparing", "Preparing"
    READY = "ready", "Ready"
    SERVED = "served", "Served"
    PAID = "paid", "Paid"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Cancelled"


class PaymentStatus(models.TextChoices):
    UNPAID = "unpaid", "Unpaid"
    PARTIAL = "partial", "Partially paid"
    PAID = "paid", "Paid"


class OrderItemStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    FIRED = "fired", "Fired"
    PREPARING = "preparing", "Preparing"
    READY = "ready", "Ready"
    SERVED = "served", "Served"
    VOIDED = "voided", "Voided"


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    CARD = "card", "Card"
    OTHER = "other", "Other"


class BillStatus(models.TextChoices):
    OPEN = "open", "Open"
    PAY_REQUESTED = "pay_requested", "Payment requested"
    PAID = "paid", "Paid"
    CLOSED = "closed", "Closed"


class BillMethod(models.TextChoices):
    """How a bill was settled. Narrower than PaymentMethod on purpose:
    `online` is Moyasar (card/mada/STC Pay), `cash`/`card` are both taken at
    the till by a cashier. The per-order Payment rows this writes still use
    PaymentMethod, so reports/ keeps working unchanged."""

    CASH = "cash", "Cash"
    CARD = "card", "Card (at the till)"
    ONLINE = "online", "Online (Moyasar)"


class Bill(models.Model):
    """
    One open bill per table session, holding every Order placed at that table
    until a cashier releases it. Payment is per *bill*, not per order — a
    table orders starters, then mains, then dessert as separate Orders and
    settles once.

    Deliberately NOT a BaseModel: a bill is a financial record, so it must
    never be soft-deleted out from under a Payment row. It carries its own
    created_at/updated_at instead.

    Totals are NEVER stored while the bill is open — orders/billing.py
    recomputes from OrderItem.price_snapshot on every read, so a voided item
    or cancelled order is reflected immediately. `total_at_payment` is only
    written at the moment of settlement, as the audit record of what was
    actually charged.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="bills")
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name="bills")
    status = models.CharField(max_length=16, choices=BillStatus.choices, default=BillStatus.OPEN)
    requested_method = models.CharField(max_length=8, choices=BillMethod.choices, blank=True)
    paid_method = models.CharField(max_length=8, choices=BillMethod.choices, blank=True)
    total_at_payment = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    pay_requested_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="settled_bills"
    )
    released_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="released_bills"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # A table can only ever have one bill that isn't finished with.
            # This is what makes get_or_create_open_bill() safe against two
            # phones scanning the same QR at the same moment — the loser of
            # the race hits this constraint rather than opening a second bill.
            models.UniqueConstraint(
                fields=["table"],
                condition=~models.Q(status="closed"),
                name="one_open_bill_per_table",
            ),
        ]

    @property
    def bill_code(self):
        return f"BILL-{self.pk:08d}" if self.pk else ""

    @property
    def is_locked(self):
        """True once the customer has asked to pay — no new orders or items
        may be added to this bill (see orders/views.py)."""
        return self.status != BillStatus.OPEN

    def __str__(self):
        return f"Bill #{self.pk} ({self.status})"


class Order(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="orders")
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    # Nullable: POS walk-in orders never belong to a table bill, and every
    # row that existed before Bill was introduced has none (backfilled for
    # live tables by migration 0007). SET_NULL so closing out a bill can
    # never cascade away order history.
    bill = models.ForeignKey("Bill", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    source = models.CharField(max_length=8, choices=OrderSource.choices, default=OrderSource.POS)
    staff = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    assigned_cashier = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="cashier_orders"
    )
    assigned_waiter = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="waiter_orders"
    )
    status = models.CharField(max_length=16, choices=OrderStatus.choices, default=OrderStatus.OPEN)
    payment_status = models.CharField(max_length=12, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    waiter_confirmed_at = models.DateTimeField(null=True, blank=True)
    waiter_confirmed_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="confirmed_orders"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=500, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    @property
    def order_code(self):
        """Stable, globally unique label suitable for receipts and KDS tickets."""
        return f"ORD-{self.pk:08d}" if self.pk else ""

    def __str__(self):
        return f"Order #{self.pk} ({self.status})"


class OrderItem(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    # Kept for traceability/reporting only — pricing always reads *_snapshot,
    # never this. See name_en_snapshot/price_snapshot below.
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_items")
    # Traceability only — never read for display or pricing. SET_NULL so
    # deleting a retired variant can't cascade away order history.
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="order_items",
    )
    name_en_snapshot = models.CharField(max_length=255)
    name_ar_snapshot = models.CharField(max_length=255)
    # The variant's name AS ORDERED. Receipts, KDS tickets and reports read
    # these, never variant.name_en — renaming "1 KG" to "Full KG" next week
    # must not rewrite last week's receipts. Blank for unvarianted products.
    variant_name_en_snapshot = models.CharField(max_length=120, blank=True)
    variant_name_ar_snapshot = models.CharField(max_length=120, blank=True)
    price_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=OrderItemStatus.choices, default=OrderItemStatus.PENDING)
    notes = models.CharField(max_length=500, blank=True)
    # Set by SendToKitchenView (pending -> fired) and OrderItemStatusView
    # (-> ready) respectively — not by hand, and never reset once set, so a
    # public client can show "cooking for Xm" / total time-to-ready.
    fired_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.quantity}x {self.name_en_snapshot}"


class Payment(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    tip = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    paid_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.amount} via {self.method}"


class OrderHistory(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="history")
    actor = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="order_events")
    event = models.CharField(max_length=40)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.order.order_code}: {self.event}"


class CustomerOrderSubmission(BaseModel):
    """One public cart submission, persisted so retries cannot append twice."""
    idempotency_key = models.CharField(max_length=128, unique=True)
    request_hash = models.CharField(max_length=64)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="customer_submissions")


class MoyasarPayment(models.Model):
    """
    One row per Moyasar payment id we have *verified against their API*.

    This is the idempotency anchor for the whole online flow: the callback
    page and the webhook both race to settle the same bill, and the UNIQUE on
    `payment_id` means only one of them can ever insert. The loser sees an
    IntegrityError and returns "already processed" without touching the bill
    or sending a second notification. See orders/billing.mark_bill_paid().

    `raw` is the response body from GET /v1/payments/{id} — never the
    callback query string and never the webhook body, neither of which is
    trusted for anything beyond supplying the id to look up.

    Not a BaseModel: a verified gateway response is an audit record and must
    not be soft-deletable.
    """

    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="moyasar_payments")
    payment_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24)
    amount_halalas = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="SAR")
    source_type = models.CharField(max_length=24, blank=True)
    source_company = models.CharField(max_length=24, blank=True)
    # Set when Moyasar reports a *paid* payment we could not reconcile with
    # the bill (wrong amount, bill already settled by another tender, bill
    # belongs to a different table). The money is real, so the row is kept
    # and cashiers are notified to refund or settle by hand — it is never
    # silently dropped. The bill is NOT marked paid in this case.
    needs_review = models.BooleanField(default=False)
    review_reason = models.CharField(max_length=120, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.payment_id} ({self.status})"
