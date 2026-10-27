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
    # Whose bill this is. Each phone number ordering at a table gets its own
    # bill, so two parties sharing a table never see or pay each other's
    # orders. Null for staff-opened (POS) table bills and for bills created
    # before per-customer bills existed.
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="bills"
    )
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
            # One unfinished bill per (table, customer). This is what makes
            # get_or_create_open_bill() safe against the same phone submitting
            # twice at the same moment — the loser of the race hits this
            # constraint rather than opening a second bill. NULLS NOT DISTINCT
            # keeps it to one staff (customer-less) bill per table as well
            # (PostgreSQL 15+; SQLite ignores the option).
            models.UniqueConstraint(
                fields=["table", "customer"],
                condition=~models.Q(status="closed"),
                name="one_open_bill_per_table_customer",
                nulls_distinct=False,
            ),
        ]
        indexes = [
            # The cashier's queue: one branch's bills that are not closed.
            models.Index(fields=["branch", "status"], name="bill_branch_status"),
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
    # What a cancellation took away, frozen at that moment: cancelling voids
    # every line, so the order's own total reads 0 afterwards and the stage
    # it was at is only in history. The cancelled-orders report reads these.
    cancelled_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancelled_orders"
    )
    cancelled_from_status = models.CharField(max_length=16, choices=OrderStatus.choices, blank=True)
    cancelled_total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Money already taken when an owner/manager cancelled a paid order: what
    # has to go back to the customer. Null when nothing had been paid.
    refunded_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta(BaseModel.Meta):
        indexes = [
            # The order list and reports: one branch's orders between two dates.
            models.Index(fields=["branch", "opened_at"], name="order_branch_opened"),
            # Queues and counts by state: open / ready / cancelled orders.
            models.Index(fields=["branch", "status"], name="order_branch_status"),
        ]

    @property
    def order_number(self):
        """The short, readable number shown on every screen, receipt and report."""
        from .codes import format_order_number
        return format_order_number(self.pk)

    @property
    def order_code(self):
        """Deprecated alias of order_number, kept so older clients still show
        the short number instead of breaking."""
        return self.order_number

    @property
    def tracking_token(self):
        """Signed, opaque lookup value for a customer's device. Never displayed."""
        if not self.pk:
            return ""
        from .codes import make_tracking_token
        return make_tracking_token(self.pk)

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
    # The kitchen station this line was sent to, fixed when it was fired
    # (kitchen/routing.fire_items) so moving a dish to another station later
    # never moves lines already cooking. Null = General.
    station = models.ForeignKey(
        "kitchen.KitchenStation", on_delete=models.SET_NULL, null=True, blank=True, related_name="order_items",
    )
    # The chef who started making this line (fired -> preparing), and when —
    # the other chefs of the station see "<name> is making this".
    started_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="started_order_items",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    # Set when a cashier or head chef sent this line to a station by hand
    # (kitchen/routing.move_line) instead of its dish's own. Firing keeps a
    # moved line's station; moving it to General is station=None with this set.
    station_moved_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="moved_order_items",
    )
    # When a station's printer screen printed this line's kitchen slip
    # (KitchenPrintView). Claimed once, so two screens at one station never
    # print it twice; cleared when the line moves to another station.
    printed_at = models.DateTimeField(null=True, blank=True)
    # When the head chef handed this ready line to the waiter at the pass
    # (OrderHandoverView). The line stays READY — the waiter still marks it
    # served at the table — it only leaves the expo screen.
    handed_over_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.quantity}x {self.name_en_snapshot}"


class Payment(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    idempotency_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    tip = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    paid_at = models.DateTimeField(auto_now_add=True)

    class Meta(BaseModel.Meta):
        indexes = [
            # sales_orders() dates every paid order by its LATEST payment — a
            # lookup run once per order in every revenue report.
            models.Index(fields=["order", "paid_at"], name="payment_order_paid"),
        ]

    def __str__(self):
        return f"{self.amount} via {self.method}"


class OrderRefund(models.Model):
    """
    Money given back for some of a paid order's lines (or part of their
    quantity), without cancelling the order. Cumulative refunds are mirrored
    on Order.refunded_amount. When the order was tax-invoiced, each refund
    gets its own ZATCA credit note for exactly these lines (zatca/pos.py).

    Not a BaseModel: a refund is a financial record and is never deleted.
    """

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    reason = models.CharField(max_length=255)
    refunded_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Refund {self.amount} on order {self.order_id}"


class OrderRefundLine(models.Model):
    refund = models.ForeignKey(OrderRefund, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="refund_lines")
    quantity = models.PositiveIntegerField()
    # The price charged (VAT inclusive), frozen like the order line's own.
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ["id"]


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
        return f"{self.order.order_number}: {self.event}"


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
