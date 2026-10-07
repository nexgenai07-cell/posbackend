from .models import OrderHistory, OrderStatus


TERMINAL_ORDER_STATUSES = {OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED}

ALLOWED_ORDER_TRANSITIONS = {
    OrderStatus.OPEN: {OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.PENDING_CASHIER: {OrderStatus.AWAITING_WAITER, OrderStatus.CANCELLED},
    OrderStatus.AWAITING_WAITER: {OrderStatus.CONFIRMED, OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.SENT, OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.SENT: {OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED, OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.READY, OrderStatus.SERVED, OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.PREPARING, OrderStatus.SERVED, OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.SERVED: {OrderStatus.PREPARING, OrderStatus.PAID, OrderStatus.PENDING_CASHIER, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.CLOSED},
    OrderStatus.CLOSED: set(),
    OrderStatus.CANCELLED: set(),
}


class InvalidOrderTransition(ValueError):
    pass


def record_order_event(order, actor, event, *, details=None, from_status="", to_status=""):
    return OrderHistory.objects.create(
        order=order,
        actor=actor,
        event=event,
        from_status=from_status,
        to_status=to_status,
        details=details or {},
    )


def transition_order(order, to_status, actor, event, *, details=None, extra_fields=None):
    from_status = order.status
    if to_status == from_status:
        return False
    if to_status not in ALLOWED_ORDER_TRANSITIONS.get(from_status, set()):
        raise InvalidOrderTransition(f"Cannot transition order from {from_status} to {to_status}")

    update_fields = ["status", "updated_at"]
    for field, value in (extra_fields or {}).items():
        setattr(order, field, value)
        update_fields.append(field)
    order.status = to_status
    order.save(update_fields=update_fields)
    record_order_event(order, actor, event, details=details, from_status=from_status, to_status=to_status)
    return True


def queue_order_for_cashier(order, actor, event, *, details=None):
    """Reopen review for a newly placed or changed set of pending items."""
    fields = {
        "assigned_cashier": None,
        "assigned_waiter": None,
        "waiter_confirmed_at": None,
        "waiter_confirmed_by": None,
    }
    if order.status == OrderStatus.PENDING_CASHIER:
        for field, value in fields.items():
            setattr(order, field, value)
        order.save(update_fields=[*fields, "updated_at"])
        record_order_event(order, actor, event, details=details)
        return
    transition_order(
        order,
        OrderStatus.PENDING_CASHIER,
        actor,
        event,
        details=details,
        extra_fields=fields,
    )
