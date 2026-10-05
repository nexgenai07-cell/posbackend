from .models import Order, OrderSource, OrderStatus

_TERMINAL_STATUSES = (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)


class BillLocked(Exception):
    """The table's bill is past `open` — the customer has asked to pay, so no
    new orders or items may be added to it. Surfaced as 409 error.billLocked."""


def get_or_create_open_order(table, staff=None, customer=None, source=OrderSource.POS):
    """
    One open (non-closed/cancelled) order per table at a time — shared by
    OrderListCreateView.post (POS, staff-initiated) and the public QR
    order-creation endpoint (no staff, source=qr). The caller must already
    hold a select_for_update() lock on `table` inside a transaction, the same
    way OrderListCreateView.post does, so two near-simultaneous requests for
    the same table can't both create one.

    Also gets-or-creates the table's open Bill and attaches the order to it,
    raising BillLocked if that bill has already moved to pay_requested/paid.
    Every order at a table therefore belongs to exactly one bill, and the lock
    is enforced in the one place both callers go through rather than being
    re-checked at each endpoint.
    """
    # Imported here, not at module scope: billing.py imports from this module's
    # siblings and would otherwise close a cycle.
    from .billing import attach_order_to_bill, get_or_create_open_bill

    bill, _ = get_or_create_open_bill(table)
    if bill.is_locked:
        raise BillLocked(bill)

    order = Order.objects.select_for_update().filter(table=table).exclude(status__in=_TERMINAL_STATUSES).order_by("-opened_at", "-id").first()
    if order:
        attach_order_to_bill(order, bill)
        return order, False
    order = Order.objects.create(
        branch=table.branch, table=table, staff=staff, customer=customer, source=source, bill=bill,
    )
    return order, True
