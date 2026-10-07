from .models import Order, OrderSource, OrderStatus

_TERMINAL_STATUSES = (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)


class BillLocked(Exception):
    """The table's bill is past `open` — the customer has asked to pay, so no
    new orders or items may be added to it. Surfaced as 409 error.billLocked."""


def get_or_create_open_order(table, staff=None, customer=None, source=OrderSource.POS, *, force_new=False):
    """
    One open (non-closed/cancelled) order per table at a time — shared by
    OrderListCreateView.post (POS, staff-initiated) and the public QR
    order-creation endpoint (no staff, source=qr). The caller must already
    hold a select_for_update() lock on `table` inside a transaction, the same
    way OrderListCreateView.post does, so two near-simultaneous requests for
    the same table can't both create one.

    Also gets-or-creates the table's live Bill and attaches the order to it.
    A paid bill can reopen while its table is still occupied; a payment-requested
    bill remains locked so its in-flight amount cannot change.
    Every order at a table therefore belongs to exactly one bill, and the lock
    is enforced in the one place both callers go through rather than being
    re-checked at each endpoint.
    """
    # Imported here, not at module scope: billing.py imports from this module's
    # siblings and would otherwise close a cycle.
    from .billing import attach_order_to_bill, get_or_create_open_bill, reopen_for_additional_items
    from .models import BillStatus

    bill, _ = get_or_create_open_bill(table)
    if bill.status == BillStatus.PAID:
        reopen_for_additional_items(bill, actor=staff)
    elif bill.is_locked:
        raise BillLocked(bill)

    order = None if force_new else Order.objects.select_for_update().filter(table=table).exclude(status__in=_TERMINAL_STATUSES).order_by("-opened_at", "-id").first()
    if order:
        attach_order_to_bill(order, bill)
        return order, False
    order = Order.objects.create(
        branch=table.branch, table=table, staff=staff, customer=customer, source=source, bill=bill,
    )
    return order, True
