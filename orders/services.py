from .models import Order, OrderSource, OrderStatus

_TERMINAL_STATUSES = (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)
_FINISHED_STATUSES = (OrderStatus.CLOSED, OrderStatus.CANCELLED)


class BillLocked(Exception):
    """The table's bill is past `open` — the customer has asked to pay, so no
    new orders or items may be added to it. Surfaced as 409 error.billLocked."""


def _open_bill_for(table, customer, staff, reopen_requested):
    """The (table, customer) bill, with the paid/pay-requested rules applied."""
    from .billing import get_or_create_open_bill, reopen_bill, reopen_for_additional_items
    from .models import BillStatus

    bill, _ = get_or_create_open_bill(table, customer)
    if bill.status == BillStatus.PAID:
        reopen_for_additional_items(bill, actor=staff)
    elif bill.status == BillStatus.PAY_REQUESTED and reopen_requested:
        # A new round changes the amount, so invalidate the current request
        # before attaching any new items. Any in-flight online verification
        # will subsequently fail the amount/status checks.
        reopen_bill(bill, actor=staff, reason="additional_items")
    elif bill.is_locked and bill.status != BillStatus.PAY_REQUESTED:
        raise BillLocked(bill)
    return bill


def get_or_create_open_order(table, staff=None, customer=None, source=OrderSource.POS, *, reopen_requested=False):
    """
    The order new items go into — shared by OrderListCreateView.post (POS,
    staff) and the public QR endpoint (customer). The caller must already hold
    a select_for_update() lock on `table` inside a transaction.

    Customer (QR) path: every phone number has its own bill at the table, and
    one order on it. The same phone keeps adding to that same order — even
    after paying, before the table is released (the order reopens and only
    the new lines are owed). Orders on any other bill, including a previous
    seating's leftovers, are never picked up.

    Staff (POS) path: the table's latest live order, whoever it belongs to,
    so the floor can add a round to it; otherwise a new staff order on the
    table's customer-less bill.
    """
    from .billing import attach_order_to_bill
    from .models import BillStatus
    from .workflow import reopen_paid_order_for_items

    if customer is not None:
        bill = _open_bill_for(table, customer, staff, reopen_requested)
        order = (
            Order.objects.select_for_update()
            .filter(table=table, bill=bill, customer=customer)
            .exclude(status__in=_FINISHED_STATUSES)
            .order_by("-opened_at", "-id")
            .first()
        )
        if order:
            reopen_paid_order_for_items(order, staff)
            return order, False
        if bill.status == BillStatus.PAY_REQUESTED:
            raise BillLocked(bill)
        order = Order.objects.create(
            branch=table.branch, table=table, staff=staff, customer=customer, source=source, bill=bill,
        )
        return order, True

    order = (
        # of=("self",): the bill filter below LEFT OUTER JOINs orders_bill
        # (bill is nullable), and PostgreSQL rejects FOR UPDATE on the nullable
        # side of an outer join — that was a 500 on POST /api/orders/. Lock
        # only the order rows. SQLite ignores row locks, so tests can't see it.
        Order.objects.select_for_update(of=("self",))
        .filter(table=table)
        .exclude(status__in=_TERMINAL_STATUSES)
        .exclude(bill__status=BillStatus.CLOSED)
        .order_by("-opened_at", "-id")
        .first()
    )
    if order:
        if order.bill_id is None:
            attach_order_to_bill(order, _open_bill_for(table, order.customer, staff, reopen_requested))
        elif order.bill.status == BillStatus.PAID:
            from .billing import reopen_for_additional_items
            reopen_for_additional_items(order.bill, actor=staff)
        return order, False
    bill = _open_bill_for(table, None, staff, reopen_requested)
    if bill.status == BillStatus.PAY_REQUESTED:
        raise BillLocked(bill)
    order = Order.objects.create(
        branch=table.branch, table=table, staff=staff, customer=None, source=source, bill=bill,
    )
    return order, True
