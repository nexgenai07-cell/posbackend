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


class Order(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="orders")
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
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
    name_en_snapshot = models.CharField(max_length=255)
    name_ar_snapshot = models.CharField(max_length=255)
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
