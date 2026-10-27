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
from django.utils.dateparse import parse_datetime
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


def get_or_create_open_bill(table, customer=None):
    """
    The open bill for this table and customer (phone), creating it on first
    order. Each phone number at a table has its own bill; `customer=None` is
    the staff (POS) bill for the table.

    The caller must already hold select_for_update() on `table` inside a
    transaction (PublicOrderCreateView and OrderListCreateView both do), which
    is what serialises two submissions at once. The
    one_open_bill_per_table_customer constraint is the backstop.
    """
    bill = (
        Bill.objects.select_for_update()
        .filter(table=table, customer=customer)
        .exclude(status=BillStatus.CLOSED)
        .first()
    )
    if bill:
        return bill, False
    return Bill.objects.create(branch=table.branch, table=table, customer=customer), True


def table_customer_access(table, phone=""):
    """Public QR access. Existing legacy owners may continue their own bill.

    Creation callers must hold the table row lock before checking this.
    Paid bills still occupy the table until staff explicitly releases it.
    """
    from customers.services import phones_match

    bills = list(Bill.objects.filter(table=table).exclude(status=BillStatus.CLOSED)
                 .select_related("customer").order_by("-id"))
    own = next((bill for bill in bills
                if bill.customer_id and phones_match(bill.customer.phone, phone)), None)
    return bool(bills), own, not bills or own is not None


def customer_bills_for_table(table, phone):
    """Unfinished bills at this table that belong to `phone` (newest first)."""
    from customers.services import phones_match

    bills = (
        Bill.objects.filter(table=table, customer__isnull=False)
        .exclude(status=BillStatus.CLOSED)
        .select_related("customer")
        .order_by("-id")
    )
    return [bill for bill in bills if phones_match(bill.customer.phone, phone)]


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
    # order.items.all(), not .exclude().exists(): read the same way as
    # order_total(), it uses the bill list's prefetched items instead of one
    # query per order.
    return [
        order for order in billable_orders(bill)
        if order.status != OrderStatus.PAID and (
            order.status != OrderStatus.SERVED
            or any(item.status not in (OrderItemStatus.SERVED, OrderItemStatus.VOIDED) for item in order.items.all())
        )
    ]


def is_ready_to_pay(bill):
    """Everything served AND something still owed — a bill whose orders are
    already paid in full is never offered as "Pay 0.00"."""
    return bool(billable_orders(bill)) and not unserved_orders(bill) and amount_outstanding(bill) > _ZERO


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
        "order_numbers": [order.order_number for order in bill.orders.all()],
        **extra,
    }


def notify_cashiers(bill, event_name, *, exclude_staff_id=None, **extra):
    """
    Fan out to every active cashier/owner/manager in the bill's branch, using
    the staff-targeted Channels groups that already exist.

    Only authenticated cashier roles can receive these: joining
    staff_<pk>_events requires a valid staff JWT (realtime/middleware.py), and
    we only ever address the pks selected by role here. A waiter or kitchen
    screen is never sent a payment event.
    """
    payload = bill_event_payload(bill, event_id=uuid.uuid4().hex, **extra)
    recipients = Staff.objects.filter(branch_id=bill.branch_id, role__in=_CASHIER_ROLES, is_active=True)
    if exclude_staff_id is not None:
        recipients = recipients.exclude(pk=exclude_staff_id)
    recipients = recipients.values_list("pk", flat=True)
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
    if unserved_orders(bill):
        raise BillTransitionError("error.billNotServed")
    if amount_outstanding(bill) <= _ZERO:
        raise BillTransitionError("error.billNothingDue")

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
            details={"bill_id": bill.pk, "method": method, "total": str(bill_total(bill)),
                     "amount_due": str(amount_outstanding(bill))},
        )

    # Only cash needs a cashier to act. An online request still notifies so
    # the floor can see the table is mid-payment, but as a plain update.
    event = "bill:cash_requested" if method == BillMethod.CASH else "bill:updated"
    notify_cashiers(bill, event, repeat=already_requested)
    logger.info("Bill %s pay requested (%s), total %s", bill.pk, method, bill_total(bill))
    return bill


def reopen_for_additional_items(bill, actor=None):
    """
    A PAID bill goes back to `open` because the guest or staff added more food
    before the table was released. Existing Payment rows remain untouched;
    the next payment only covers orders that do not already have a payment.

    The guest orders a coffee after settling: the money already taken stays
    recorded (its Payment rows and total_at_payment are untouched), the bill
    reopens, and what is now owed is the NEW total minus what was already
    paid — see amount_outstanding() below.

    This preserves payment history in the order Payment rows, while allowing
    another order to be added. The outstanding amount is current bill total
    minus those recorded payments.
    """
    if bill.status == BillStatus.CLOSED:
        raise BillTransitionError("error.billClosed")
    if bill.status != BillStatus.PAID:
        # Nothing to reconcile — an open or pay_requested bill is handled by
        # the ordinary lock rules.
        return bill

    bill.status = BillStatus.OPEN
    bill.requested_method = ""
    bill.pay_requested_at = None
    bill.save(update_fields=["status", "requested_method", "pay_requested_at", "updated_at"])

    if bill.table_id:
        bill.table.open()

    for order in billable_orders(bill):
        record_order_event(
            order, actor, "bill_reopened_for_items",
            details={"bill_id": bill.pk, "already_paid": str(amount_paid(bill))},
        )
    notify_cashiers(bill, "bill:updated", reopened=True)
    logger.info(
        "Bill %s reopened for additional items by staff %s (already paid %s)",
        bill.pk, getattr(actor, "pk", None), amount_paid(bill),
    )
    return bill


def payment_request_cancelled_by_add(bill):
    """True when the bill's latest payment request was cancelled because new
    items were added — the customer must request payment again once served."""
    history = [
        entry
        for order in bill.orders.all()
        for entry in order.history.all()
        if entry.event in ("bill_pay_requested", "bill_pay_cancelled")
        and (entry.details or {}).get("bill_id") == bill.pk
    ]
    if not history:
        return False
    latest = max(history, key=lambda entry: (entry.created_at, entry.pk))
    return latest.event == "bill_pay_cancelled" and (latest.details or {}).get("reason") == "additional_items"


def order_paid(order):
    """Money actually recorded against one order (Payment rows)."""
    return sum((payment.amount for payment in order.payments.all()), _ZERO)


def order_due(order):
    """What one order still owes. Never negative: an overpayment is a refund
    decision for a human, not credit against another order."""
    return max(order_total(order) - order_paid(order), _ZERO)


def amount_paid(bill):
    """What has actually been taken for this bill's billable orders.

    Payments on cancelled orders are left out — that money is a refund
    question, and counting it would silently discount someone else's food."""
    return sum((order_paid(order) for order in billable_orders(bill)), _ZERO)


def amount_outstanding(bill):
    """
    What is still owed: the sum of each billable order's own unpaid balance.

    Computed per order (not bill total minus everything paid) so a payment
    taken at the till for one order can never hide what another order owes.
    """
    return sum((order_due(order) for order in billable_orders(bill)), _ZERO)


def reopen_bill(bill, actor=None, *, reason="customer_cancelled"):
    """
    pay_requested -> open. The unlock for a stuck bill: a customer taps Pay by
    mistake, or picks cash and then wants to add dessert. Never allowed once
    paid — money has moved by then and the fix is a refund, not a state change.
    """
    if bill.status == BillStatus.OPEN:
        return bill
    if bill.status in (BillStatus.PAID, BillStatus.CLOSED):
        raise BillTransitionError("error.billAlreadyPaid")

    previous_method = bill.requested_method
    previous_total = bill_total(bill)
    bill.status = BillStatus.OPEN
    bill.requested_method = ""
    bill.pay_requested_at = None
    bill.save(update_fields=["status", "requested_method", "pay_requested_at", "updated_at"])

    if bill.table_id:
        bill.table.open()  # back to `occupied` — the table is still seated

    for order in billable_orders(bill):
        record_order_event(order, actor, "bill_pay_cancelled", details={"bill_id": bill.pk, "reason": reason})

    notify_cashiers(
        bill,
        "bill:pay_cancelled",
        cancelled_method=previous_method,
        cancelled_total=str(previous_total),
        reason=reason,
        exclude_staff_id=getattr(actor, "pk", None),
    )
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


def close_bills_for_released_table(table, actor):
    """
    Staff released the table itself (POST /tables/<id>/close/): end every bill
    still on it, so customers' phones see the session end and the next party
    — even with the same phone — starts a fresh bill.

    The caller must hold the table lock and have checked that no order on the
    table is still owed (only paid/closed/cancelled remain), so nothing here
    can drop unpaid food. Without this the table went empty while its bills
    stayed open/paid forever.
    """
    now = timezone.now()
    bills = Bill.objects.select_for_update().filter(table=table).exclude(status=BillStatus.CLOSED)
    for bill in bills:
        bill.status = BillStatus.CLOSED
        bill.closed_at = now
        bill.released_by = actor
        bill.save(update_fields=["status", "closed_at", "released_by", "updated_at"])
        for order in bill.orders.all():
            if order.status == OrderStatus.PAID:
                transition_order(order, OrderStatus.CLOSED, actor, "bill_released",
                                 details={"bill_id": bill.pk}, extra_fields={"closed_at": now})
        transaction.on_commit(lambda bill=bill: notify_cashiers(bill, "bill:released"))
        logger.info("Bill %s closed by table release (staff %s)", bill.pk, getattr(actor, "pk", None))


def _is_empty_order(order):
    """Nothing to cook and nothing paid: every line was removed."""
    return (
        not order.items.exclude(status=OrderItemStatus.VOIDED).exists()
        and not order.payments.exists()
    )


def delete_if_draft(order):
    """
    Delete a staff order that never became anything: no customer, never past
    cashier review, no line ever (not even a voided one), no payment. That is
    a POS ticket whose every line was taken off again — keeping it would leave
    an order number with nothing in it. Its bill goes too when nothing else is
    on it. Returns True if deleted. The caller must hold the order lock.
    """
    if (
        order.customer_id is not None
        or order.status not in (OrderStatus.OPEN, OrderStatus.PENDING_CASHIER)
        or order.items.exists()
        or order.payments.exists()
        or order.refunds.exists()
    ):
        return False
    bill_id = order.bill_id
    order.delete()
    if bill_id:
        Bill.objects.filter(pk=bill_id, orders__isnull=True, moyasar_payments__isnull=True).delete()
    return True


def live_table_orders(table):
    """The table's orders that are not paid/closed/cancelled, oldest first."""
    return list(
        Order.objects.select_for_update().filter(table=table)
        .exclude(status__in=[OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED])
        .order_by("opened_at")
    )


def discard_empty_orders(table, actor):
    """
    Remove the table's live orders that hold nothing (see _is_empty_order)
    and return the live orders that remain. An empty order owes nothing, so
    it must not keep the table occupied or block Release / delete. A draft is
    deleted (delete_if_draft); anything else empty — e.g. a customer's QR
    order whose lines were all removed — is cancelled, keeping its record.
    The caller must hold the table lock.
    """
    remaining = []
    for order in live_table_orders(table):
        if not _is_empty_order(order):
            remaining.append(order)
            continue
        if delete_if_draft(order):
            continue
        # No cancelled_at: nothing was ordered, so this is not a cancellation
        # for the cancelled-orders report (it filters on cancelled_at).
        transition_order(order, OrderStatus.CANCELLED, actor, "empty_order_discarded",
                         extra_fields={"cancelled_by": actor, "cancelled_from_status": order.status})
    return remaining


def free_table_if_nothing_on_it(table):
    """
    The last line came off the table's order: nothing is left to cook or pay,
    so the table is no longer occupied; adding again re-occupies it. The
    caller must hold the table lock.
    """
    from tables.models import TableStatus

    if table.status == TableStatus.EMPTY:
        return
    if all(_is_empty_order(order) for order in live_table_orders(table)) and not (
        Order.objects.filter(table=table, status=OrderStatus.PAID).exclude(bill__status=BillStatus.CLOSED).exists()
    ):
        table.close()


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

        # One tax invoice for the orders this settlement paid (after commit;
        # orders already invoiced at the till are skipped there).
        from zatca import pos as zatca_pos
        zatca_pos.orders_paid(
            [order.pk for order in billable_orders(bill) if order.payment_status == PaymentStatus.PAID],
            actor=actor, moyasar=moyasar,
        )

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
    expected = to_halalas(amount_outstanding(bill))

    # Keep an auditable provider snapshot without persisting arbitrary nested
    # response fields (for example card fingerprints or customer details).
    # Settlement logic uses the verified values above and the normalized DB
    # columns; metadata.bill_id is retained for reconciliation.
    metadata = moyasar.get("metadata") if isinstance(moyasar.get("metadata"), dict) else {}
    raw_snapshot = {
        key: moyasar[key]
        for key in ("id", "status", "amount", "currency", "created_at", "paid_at")
        if moyasar.get(key) is not None
    }
    source_snapshot = moyasar.get("source") if isinstance(moyasar.get("source"), dict) else {}
    if source_snapshot:
        raw_snapshot["source"] = {
            key: source_snapshot[key]
            for key in ("type", "company")
            if source_snapshot.get(key) is not None
        }
    if metadata.get("bill_id") is not None:
        raw_snapshot["metadata"] = {"bill_id": metadata["bill_id"]}
        if metadata.get("payment_request_at") is not None:
            raw_snapshot["metadata"]["payment_request_at"] = metadata["payment_request_at"]

    review_reason = ""
    if bill.status != BillStatus.PAY_REQUESTED:
        review_reason = f"bill is {bill.status}; no active payment request"
    elif not metadata.get("payment_request_at"):
        review_reason = "payment metadata has no payment request timestamp"
    elif parse_datetime(str(metadata["payment_request_at"])) != bill.pay_requested_at:
        review_reason = "payment belongs to a previous request"
    elif amount != expected:
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
                raw=raw_snapshot,
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
    One Payment row per billable order for exactly what that order still
    owed, so reports/, the POS receipt and OrderDetailsPage keep reading
    orders.Payment — and the rows add up to the money actually collected.

    Orders already paid in full (at the till, or by an earlier settlement of
    this bill) get no new row. The key carries the settlement time, so a
    reopened bill's second settlement can record the new items' money while
    the bill row lock and the paid/closed guard in mark_bill_paid() still stop
    the same settlement being written twice.
    """
    payment_method = _PAYMENT_METHOD_FOR[method]
    stamp = int(now.timestamp() * 1_000_000)
    for order in billable_orders(bill):
        due = order_due(order)
        if due > _ZERO:
            Payment.objects.create(
                order=order, amount=due, method=payment_method,
                idempotency_key=f"bill-{bill.pk}-order-{order.pk}-{stamp}",
            )
            order._prefetched_objects_cache = {}
        _mark_order_paid_if_settled(order, actor, bill, method, recorded=due)


def _mark_order_paid_if_settled(order, actor, bill, method, recorded=_ZERO):
    """Order fully paid -> payment_status paid, and served -> paid."""
    if order_due(order) > _ZERO:
        if order_paid(order) > _ZERO and order.payment_status != PaymentStatus.PARTIAL:
            order.payment_status = PaymentStatus.PARTIAL
            order.save(update_fields=["payment_status", "updated_at"])
        return
    if order.status == OrderStatus.SERVED:
        transition_order(
            order, OrderStatus.PAID, actor, "bill_settled",
            details={"bill_id": bill.pk, "method": method, "amount": str(recorded)},
            extra_fields={"payment_status": PaymentStatus.PAID},
        )
    elif order.payment_status != PaymentStatus.PAID:
        order.payment_status = PaymentStatus.PAID
        order.save(update_fields=["payment_status", "updated_at"])


_BILL_METHOD_FOR_PAYMENT = {
    PaymentMethod.CASH: BillMethod.CASH,
    PaymentMethod.CARD: BillMethod.CARD,
    PaymentMethod.OTHER: BillMethod.CARD,
}


def settle_bill_if_covered(bill_id, payment_method, actor=None):
    """
    Mark a bill paid when payments taken order-by-order at the till already
    cover it — without writing any further Payment rows.

    Called after OrderPaymentsView records a till payment. Without this the
    bill stayed `open` with nothing left to pay, and a later bill settlement
    wrote the same orders' money a second time.
    """
    with transaction.atomic():
        bill = Bill.objects.select_for_update().get(pk=bill_id)
        if bill.status not in (BillStatus.OPEN, BillStatus.PAY_REQUESTED):
            return False
        if not billable_orders(bill) or unserved_orders(bill) or amount_outstanding(bill) > _ZERO:
            return False
        now = timezone.now()
        bill.status = BillStatus.PAID
        bill.paid_method = _BILL_METHOD_FOR_PAYMENT.get(payment_method, BillMethod.CASH)
        bill.total_at_payment = bill_total(bill)
        bill.paid_at = now
        bill.paid_by = actor
        bill.save(update_fields=["status", "paid_method", "total_at_payment", "paid_at", "paid_by", "updated_at"])
        if bill.table_id:
            bill.table.mark_needs_bill()
        transaction.on_commit(lambda: notify_cashiers(
            Bill.objects.prefetch_related("orders").get(pk=bill.pk), "bill:paid"
        ))
        logger.info("Bill %s settled by till payments", bill.pk)
        return True
