from .models import Order, OrderSource, OrderStatus

_TERMINAL_STATUSES = (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)


def get_or_create_open_order(table, staff=None, customer=None, source=OrderSource.POS):
    """
    One open (non-closed/cancelled) order per table at a time — shared by
    OrderListCreateView.post (POS, staff-initiated) and the public QR
    order-creation endpoint (no staff, source=qr). The caller must already
    hold a select_for_update() lock on `table` inside a transaction, the same
    way OrderListCreateView.post does, so two near-simultaneous requests for
    the same table can't both create one.
    """
    order = Order.objects.select_for_update().filter(table=table).exclude(status__in=_TERMINAL_STATUSES).order_by("-opened_at", "-id").first()
    if order:
        return order, False
    order = Order.objects.create(branch=table.branch, table=table, staff=staff, customer=customer, source=source)
    return order, True
