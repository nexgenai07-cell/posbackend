"""
Bill totals, the bill state machine, and the one atomic path to "paid".

Sibling of workflow.py: that file owns the Order state machine, this one owns
the Bill state machine. Nothing outside here may set Bill.status.

The single rule this module exists to enforce: **the amount is computed from
the database, every time, and a client-supplied amount is never read.** The
customer's browser is told what to pay; it is never asked.
"""

import logging
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import Staff, StaffRole
from realtime.publisher import publish_event, publish_staff_event

from .models import (
    Bill,
    BillMethod,
    BillStatus,
    MoyasarPayment,
    Order,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from .workflow import record_order_event, transition_order

logger = logging.getLogger("orders.billing")

_ZERO = Decimal("0.00")
_CENTS = Decimal("0.01")

# Orders that no longer count toward what the table owes.
_UNBILLED_ORDER_STATUSES = (OrderStatus.CANCELLED,)

# Which PaymentMethod each BillMethod writes onto the per-order Payment rows.
# `online` maps to `card` because that is what the till actually took through
# Moyasar, and reports/ only knows PaymentMethod.
_PAYMENT_METHOD_FOR = {
    BillMethod.CASH: PaymentMethod.CASH,
    BillMethod.CARD: PaymentMethod.CARD,
    BillMethod.ONLINE: PaymentMethod.CARD,
}


class BillTransitionError(ValueError):
    """Attempted an illegal bill state change. Carries an error.* code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# -- totals -----------------------------------------------------------------


def billable_orders(bill):
    """Orders on this bill that the customer still owes for."""
    return [order for order in bill.orders.all() if order.status not in _UNBILLED_ORDER_STATUSES]


def order_total(order):
    """One order's total, excluding voided items. Decimal throughout — a
    float would quietly lose halalas on a long bill."""
    return sum(
        (item.price_snapshot * item.quantity
         for item in order.items.all()
         if item.status != OrderItemStatus.VOIDED),
        _ZERO,
    )


def bill_total(bill):
    """
    What this table owes right now, recomputed from OrderItem.price_snapshot
    on every call. Cancelled orders and voided items are excluded, so a
    cashier voiding a line immediately lowers the amount the customer is asked
    to pay — and, because verification re-runs this under a row lock, also
    immediately invalidates an in-flight payment for the old amount.
    """
    return sum((order_total(order) for order in billable_orders(bill)), _ZERO)


def to_halalas(amount):
    """
    Decimal SAR -> integer halalas, which is the only unit Moyasar accepts.

    Quantised to 2dp first so a price like 10.005 can't round differently here
    than it does on a receipt. int() on the product of two Decimals is exact —
    there is deliberately no float anywhere in this path.
    """
    return int((amount.quantize(_CENTS) * 100).to_integral_value())


def bill_total_halalas(bill):
    return to_halalas(bill_total(bill))


# -- lookup / creation ------------------------------------------------------


def get_or_create_open_bill(table):
    """
    The one open bill for this table, creating it on first order.

    The caller must already hold select_for_update() on `table` inside a
    transaction (PublicOrderCreateView and OrderListCreateView both do), which
    is what serialises two phones scanning the same QR at once. The
    one_open_bill_per_table constraint is the backstop if a caller forgets.
    """
    bill = Bill.objects.select_for_update().filter(table=table).exclude(status=BillStatus.CLOSED).first()
    if bill:
        return bill, False
    return Bill.objects.create(branch=table.branch, table=table), True


def current_bill_for_table(table):
    """The open/pay_requested/paid bill for a table, or None. Read-only."""
    return (
        Bill.objects.filter(table=table)
        .exclude(status=BillStatus.CLOSED)
        .prefetch_related("orders__items")
        .first()
    )


def attach_order_to_bill(order, bill):
    if order.bill_id != bill.pk:
        order.bill = bill
        order.save(update_fields=["bill", "updated_at"])


# -- readiness --------------------------------------------------------------


def unserved_orders(bill):
    """
    Billable orders that have not reached `served` yet.

    Payment is only offered once the table has everything it ordered: the
    Order state machine (workflow.py) allows served -> paid and nothing else,
    and we deliberately did not loosen that. If food is still coming, the
    customer is told to wait rather than being handed a form that would strand
    their money against an order that cannot transition.
    """
    return [order for order in billable_orders(bill) if order.status != OrderStatus.SERVED]


def is_ready_to_pay(bill):
    return bool(billable_orders(bill)) and not unserved_orders(bill)


# -- notifications ----------------------------------------------------------

_CASHIER_ROLES = (StaffRole.CASHIER, StaffRole.OWNER, StaffRole.MANAGER)


def bill_event_payload(bill, **extra):
    table = bill.table
    return {
        "id": bill.pk,
        "bill_id": bill.pk,
        "bill_code": bill.bill_code,
        "table_id": bill.table_id,
        "table_label": table.label_en if table else None,
        "status": bill.status,
        "method": bill.paid_method or bill.requested_method or "",
        "total": str(bill.total_at_payment if bill.total_at_payment is not None else bill_total(bill)),
        "order_ids": [order.pk for order in bill.orders.all()],
        **extra,
    }


def notify_cashiers(bill, event_name, **extra):
    """
    Fan out to every active cashier/owner/manager in the bill's branch, using
    the staff-targeted Channels groups that already exist.

    Only authenticated cashier roles can receive these: joining
    staff_<pk>_events requires a valid staff JWT (realtime/middleware.py), and
    we only ever address the pks selected by role here. A waiter or kitchen
    screen is never sent a payment event.
    """
    payload = bill_event_payload(bill, event_id=uuid.uuid4().hex, **extra)
    recipients = Staff.objects.filter(
        branch_id=bill.branch_id, role__in=_CASHIER_ROLES, is_active=True
    ).values_list("pk", flat=True)
    for staff_id in recipients:
        publish_staff_event(staff_id, event_name, payload)
    # Also to the table's own group, so the customer's phone can react without
    # waiting for its next poll (burger_web polls today; this makes a push
    # upgrade a frontend-only change later).
    publish_event(bill.branch_id, "bill:updated", bill.pk, table_id=bill.table_id)


# -- state machine ----------------------------------------------------------


def request_payment(bill, method, actor=None):
    """open -> pay_requested. Caller must hold a lock on `bill`."""
    if bill.status == BillStatus.PAID:
        raise BillTransitionError("error.billAlreadyPaid")
    if bill.status == BillStatus.CLOSED:
        raise BillTransitionError("error.billClosed")
    if not billable_orders(bill):
        raise BillTransitionError("error.billEmpty")
    if not is_ready_to_pay(bill):
        raise BillTransitionError("error.billNotServed")

    already_requested = bill.status == BillStatus.PAY_REQUESTED
    bill.status = BillStatus.PAY_REQUESTED
    bill.requested_method = method
    bill.pay_requested_at = bill.pay_requested_at or timezone.now()
    bill.save(update_fields=["status", "requested_method", "pay_requested_at", "updated_at"])

    if bill.table_id:
        bill.table.mark_needs_bill()

    for order in billable_orders(bill):
        record_order_event(
            order, actor, "bill_pay_requested",
            details={"bill_id": bill.pk, "method": method, "total": str(bill_total(bill))},
        )

    # Only cash needs a cashier to act. An online request still notifies so
    # the floor can see the table is mid-payment, but as a plain update.
    event = "bill:cash_requested" if method == BillMethod.CASH else "bill:updated"
    notify_cashiers(bill, event, repeat=already_requested)
    logger.info("Bill %s pay requested (%s), total %s", bill.pk, method, bill_total(bill))
    return bill


def reopen_bill(bill, actor=None):
    """
    pay_requested -> open. The unlock for a stuck bill: a customer taps Pay by
    mistake, or picks cash and then wants to add dessert. Never allowed once
    paid — money has moved by then and the fix is a refund, not a state change.
    """
    if bill.status == BillStatus.OPEN:
        return bill
    if bill.status in (BillStatus.PAID, BillStatus.CLOSED):
        raise BillTransitionError("error.billAlreadyPaid")

    bill.status = BillStatus.OPEN
    bill.requested_method = ""
    bill.pay_requested_at = None
    bill.save(update_fields=["status", "requested_method", "pay_requested_at", "updated_at"])

    if bill.table_id:
        bill.table.open()  # back to `occupied` — the table is still seated

    for order in billable_orders(bill):
        record_order_event(order, actor, "bill_pay_cancelled", details={"bill_id": bill.pk})

    notify_cashiers(bill, "bill:updated")
    logger.info("Bill %s reopened by %s", bill.pk, getattr(actor, "pk", "customer"))
    return bill


def release_table(bill, actor):
    """paid -> closed, and the table goes back to empty."""
    if bill.status != BillStatus.PAID:
        raise BillTransitionError("error.billNotPaid")

    now = timezone.now()
    bill.status = BillStatus.CLOSED
    bill.closed_at = now
    bill.released_by = actor
    bill.save(update_fields=["status", "closed_at", "released_by", "updated_at"])

    for order in bill.orders.all():
        if order.status == OrderStatus.PAID:
            transition_order(order, OrderStatus.CLOSED, actor, "bill_released",
                             details={"bill_id": bill.pk}, extra_fields={"closed_at": now})

    if bill.table_id:
        # Note: Table.close() only flips status to `empty` — it does NOT
        # rotate session_token, so the printed QR keeps working and the next
        # party at this table opens a fresh Bill from the same code. That is
        # intended for a permanent printed QR; the Bill row, not the token, is
        # what separates one seating from the next.
        bill.table.close()

    notify_cashiers(bill, "bill:released")
    logger.info("Bill %s released by staff %s", bill.pk, getattr(actor, "pk", None))
    return bill


# -- the one way a bill becomes paid ----------------------------------------

SETTLED = "settled"
ALREADY_PROCESSED = "already_processed"
NEEDS_REVIEW = "needs_review"


def mark_bill_paid(bill_id, method, actor=None, moyasar=None):
    """
    The single atomic, idempotent path to `paid`. Every caller goes through
    here: the callback verification, the webhook, and the cashier's manual
    cash/card settle.

    Three independent guards make a double callback+webhook safe:
      1. MoyasarPayment.payment_id is UNIQUE — only one inserter wins.
      2. The bill is re-read under select_for_update(), so the two racers
         serialise rather than interleave.
      3. A bill already in paid/closed returns ALREADY_PROCESSED untouched.

    Notifications fire from transaction.on_commit, so a rolled-back settle can
    never emit a phantom "Table X PAID".

    `moyasar` is the *verified* API response dict (never a webhook body).
    Returns (outcome, bill).
    """
    with transaction.atomic():
        # No select_related() here. Bill.table is nullable, so select_related
        # would make it a LEFT OUTER JOIN, and Postgres refuses FOR UPDATE on
        # the nullable side of an outer join ("FOR UPDATE cannot be applied to
        # the nullable side of an outer join"). SQLite silently ignores
        # select_for_update entirely, which is why the test suite cannot catch
        # this — it only shows up on the real database. The row lock matters
        # far more than saving two lazy-loaded queries.
        bill = Bill.objects.select_for_update().get(pk=bill_id)

        if moyasar is not None:
            outcome = _record_moyasar_payment(bill, moyasar)
            if outcome is not None:
                return outcome, bill

        if bill.status in (BillStatus.PAID, BillStatus.CLOSED):
            logger.info("Bill %s already settled; ignoring duplicate %s settle", bill.pk, method)
            return ALREADY_PROCESSED, bill

        total = bill_total(bill)
        now = timezone.now()
        bill.status = BillStatus.PAID
        bill.paid_method = method
        bill.total_at_payment = total
        bill.paid_at = now
        bill.paid_by = actor
        bill.save(update_fields=[
            "status", "paid_method", "total_at_payment", "paid_at", "paid_by", "updated_at",
        ])

        _write_order_payments(bill, method, actor, now)

        if bill.table_id:
            bill.table.mark_needs_bill()  # paid but not yet released by a cashier

        transaction.on_commit(lambda: notify_cashiers(
            Bill.objects.prefetch_related("orders").get(pk=bill.pk), "bill:paid"
        ))
        logger.info("Bill %s settled via %s for %s SAR", bill.pk, method, total)
        return SETTLED, bill


def _record_moyasar_payment(bill, moyasar):
    """
    Insert the verified payment row, or return a terminal outcome.

    Returns None to mean "keep going and settle the bill". Anything else is
    the final answer for this call.
    """
    payment_id = str(moyasar.get("id"))
    amount = int(moyasar.get("amount") or 0)
    source = moyasar.get("source") or {}
    expected = bill_total_halalas(bill)

    review_reason = ""
    if amount != expected:
        review_reason = f"amount {amount} != bill total {expected}"
    elif bill.status in (BillStatus.PAID, BillStatus.CLOSED):
        review_reason = f"bill already {bill.status}"

    try:
        with transaction.atomic():
            MoyasarPayment.objects.create(
                bill=bill,
                payment_id=payment_id,
                status=str(moyasar.get("status", "")),
                amount_halalas=amount,
                currency=str(moyasar.get("currency") or settings.BILL_CURRENCY),
                source_type=str(source.get("type") or "")[:24],
                source_company=str(source.get("company") or "")[:24],
                needs_review=bool(review_reason),
                review_reason=review_reason[:120],
                raw=moyasar,
            )
    except IntegrityError:
        # The other racer (webhook vs callback) already inserted this exact
        # payment id. It either settled the bill or flagged it; either way this
        # call must do nothing further and send no second notification.
        logger.info("Moyasar payment %s already recorded; ignoring duplicate", payment_id)
        return ALREADY_PROCESSED

    if review_reason:
        # Moyasar says the customer's money moved, but we cannot reconcile it
        # with this bill. Never silently drop it: the row is kept, the bill is
        # left unpaid, and cashiers are told to refund or settle by hand.
        logger.error(
            "Moyasar payment %s for bill %s needs manual review: %s",
            payment_id, bill.pk, review_reason,
        )
        transaction.on_commit(lambda: notify_cashiers(
            bill, "bill:needs_review", payment_id=payment_id, reason=review_reason,
        ))
        return NEEDS_REVIEW

    return None


def _write_order_payments(bill, method, actor, now):
    """
    One Payment row per billable order, so reports/, the POS receipt and
    OrderDetailsPage all keep working unchanged — they read orders.Payment and
    know nothing about bills.

    The idempotency_key is deterministic (`bill-<id>-order-<id>`), so if this
    ever runs twice the UNIQUE constraint makes the second pass a no-op rather
    than doubling the day's takings.
    """
    payment_method = _PAYMENT_METHOD_FOR[method]
    for order in billable_orders(bill):
        amount = order_total(order)
        _, created = Payment.objects.get_or_create(
            idempotency_key=f"bill-{bill.pk}-order-{order.pk}",
            defaults={"order": order, "amount": amount, "method": payment_method},
        )
        if not created:
            continue
        order.payment_status = PaymentStatus.PAID
        if order.status == OrderStatus.SERVED:
            transition_order(
                order, OrderStatus.PAID, actor, "bill_settled",
                details={"bill_id": bill.pk, "method": method, "amount": str(amount)},
                extra_fields={"payment_status": PaymentStatus.PAID},
            )
        else:
            # request_payment() refuses to leave `open` unless every billable
            # order is served, so this is unreachable through the API. It can
            # only happen if an order was moved backwards in Django admin
            # between the request and the settle — record the money, leave the
            # order status alone rather than forcing an illegal transition.
            order.save(update_fields=["payment_status", "updated_at"])
            record_order_event(
                order, actor, "bill_settled_unserved",
                details={"bill_id": bill.pk, "order_status": order.status},
            )
            logger.warning(
                "Bill %s settled while order %s was %s, not served",
                bill.pk, order.pk, order.status,
            )
