from .models import OrderHistory, OrderItemStatus, OrderStatus


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
    # PAID -> SERVED: the same customer added items after paying, before the
    # table was released; the order reopens and only the new lines are owed.
    OrderStatus.PAID: {OrderStatus.CLOSED, OrderStatus.SERVED},
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


def reopen_paid_order_for_items(order, actor):
    """A paid (not yet released) order takes more items: back to `served`
    with payment `partial`, so the new lines go through review/kitchen and
    the customer then pays only for them."""
    from .models import PaymentStatus

    if order.status != OrderStatus.PAID:
        return False
    return transition_order(
        order, OrderStatus.SERVED, actor, "paid_order_reopened_for_items",
        extra_fields={"payment_status": PaymentStatus.PARTIAL},
    )


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


def recompute_order_status_from_items(order, actor, *, event="kitchen_status_changed"):
    """Keep the parent status aligned with every non-voided line, including new batches."""
    items = [item for item in order.items.all() if item.status != OrderItemStatus.VOIDED]
    if not items:
        return

    statuses = [item.status for item in items]
    if OrderItemStatus.PENDING in statuses:
        # A fresh line on an already-ready/served order means work has resumed.
        if order.status in (OrderStatus.READY, OrderStatus.SERVED):
            transition_order(order, OrderStatus.PREPARING, actor, event)
        return

    if all(value == OrderItemStatus.SERVED for value in statuses):
        next_status = OrderStatus.SERVED
    elif all(value in (OrderItemStatus.READY, OrderItemStatus.SERVED) for value in statuses):
        next_status = OrderStatus.READY
    elif OrderItemStatus.PREPARING in statuses or any(
        value in (OrderItemStatus.READY, OrderItemStatus.SERVED) for value in statuses
    ):
        # A new fired/preparing line mixed with an older ready/served line is
        # an active batch, even though the older line keeps its own status.
        next_status = OrderStatus.PREPARING
    elif OrderItemStatus.FIRED in statuses:
        next_status = OrderStatus.SENT
    else:
        return

    if order.status != next_status:
        transition_order(order, next_status, actor, event)
