from decimal import Decimal
from datetime import datetime, time, timedelta
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.mixins import ListModelMixin
from rest_framework.response import Response
from rest_framework.views import APIView

from rest_framework.permissions import AllowAny

from accounts.models import Staff, StaffRole
from accounts.permissions import IsCashierStaff, IsKitchenStaff, IsOrderStaff, IsPOSStaff, IsWaiterStaff
from catalog.services import is_available_today
from common.geofence import validate_branch_location
from customers.models import Customer
from customers.services import find_or_create_customer
from inventory.services import ProductStockUnavailable, check_product_stock, deduct_stock_for_order_items
from tables.models import Table
from tables.services import parse_qr_payload

from realtime.publisher import publish_staff_event

from .models import CustomerOrderSubmission, Order, OrderHistory, OrderItem, OrderItemStatus, OrderSource, OrderStatus, Payment, PaymentStatus
from .serializers import (
    AddOrderItemSerializer,
    OrderItemSerializer,
    OrderHistorySerializer,
    KitchenTicketOrderSerializer,
    OrderSerializer,
    PaymentSerializer,
    PublicOrderSerializer,
    RecordPaymentSerializer,
)
from .services import BillLocked, get_or_create_open_order
from .workflow import (
    InvalidOrderTransition,
    TERMINAL_ORDER_STATUSES,
    queue_order_for_cashier,
    record_order_event,
    transition_order,
)

_TERMINAL_STATUSES = tuple(TERMINAL_ORDER_STATUSES)


def _closed_order_response(order):
    """None if the order is still open for changes, else the 409 to return."""
    if order.status in _TERMINAL_STATUSES:
        return Response({"error": "error.orderClosed"}, status=status.HTTP_409_CONFLICT)
    return None


def _bill_locked_response():
    """409 for "this table has asked to pay, so nothing new goes on the bill".

    Distinct from error.orderClosed: the order itself may be perfectly open,
    it is the *bill* that is locked, and the fix is a cashier reopening it
    (POST /api/bills/<id>/reopen/) rather than starting a new order."""
    return Response({"error": "error.billLocked"}, status=status.HTTP_409_CONFLICT)


def _locked_bill_for_order(order, actor=None):
    """
    The order's bill if staff may NOT add to it, else None.

    Two different situations, deliberately treated differently:

      pay_requested  the customer is mid-payment and the amount must not move
                     under them -> blocked, a cashier reopens first.

      paid           the money is in, but the table has not been released and
                     the guest has ordered a coffee. Staff may add: the bill
                     reopens and the guest pays the difference. Blocking here
                     would force a void-and-rekey for one extra item.

    `closed` means the table is already released, so that is blocked too.
    """
    from .billing import reopen_for_additional_items
    from .models import BillStatus

    if order.bill_id is None:
        return None
    bill = order.bill
    if bill.status == BillStatus.OPEN:
        return None
    if bill.status == BillStatus.PAID:
        reopen_for_additional_items(bill, actor=actor)
        return None
    return bill


def _order_queryset():
    return Order.objects.select_related(
        "table", "customer", "staff", "assigned_cashier", "assigned_waiter", "waiter_confirmed_by"
    ).prefetch_related("items", "payments", "history")


def _get_table_or_404(pk, branch):
    return get_object_or_404(Table, pk=pk, branch=branch)


def _get_order_or_404(pk, branch):
    """Every order lookup is scoped to the requester's branch — an owner of
    branch A must never be able to read or mutate branch B's orders just by
    guessing an id."""
    return get_object_or_404(_order_queryset(), pk=pk, branch=branch)


def _transition_or_conflict(order, to_status, actor, event, *, details=None, extra_fields=None):
    try:
        transition_order(order, to_status, actor, event, details=details, extra_fields=extra_fields)
    except InvalidOrderTransition:
        return Response(
            {"error": "error.orderTransitionInvalid", "status": order.status, "requested": to_status},
            status=status.HTTP_409_CONFLICT,
        )
    return None


def _billable_items(order):
    return [item for item in order.items.all() if item.status != OrderItemStatus.VOIDED]


def _order_total(order):
    return sum((item.price_snapshot * item.quantity for item in _billable_items(order)), Decimal("0.00"))


def _set_payment_status(order, value, actor, event, details=None):
    previous = order.payment_status
    order.payment_status = value
    order.save(update_fields=["payment_status", "updated_at"])
    record_order_event(order, actor, event, details=details or {"from": previous, "to": value})


def _dispatch_confirmed_order(order, actor, *, move_order_to_sent=True):
    """Fire only pending items after waiter confirmation; unconfirmed items stay off KDS."""
    pending_items = list(
        order.items.select_for_update().filter(status=OrderItemStatus.PENDING).select_related("product")
    )
    if not pending_items:
        return Response({"error": "error.noPendingItems"}, status=status.HTTP_400_BAD_REQUEST)
    deduct_stock_for_order_items(pending_items)
    OrderItem.objects.filter(pk__in=[item.pk for item in pending_items]).update(
        status=OrderItemStatus.FIRED, fired_at=timezone.now()
    )
    details = {"item_count": len(pending_items)}
    if move_order_to_sent:
        conflict = _transition_or_conflict(order, OrderStatus.SENT, actor, "sent_to_kitchen", details=details)
        if conflict:
            return conflict
    else:
        record_order_event(order, actor, "additional_items_sent_to_kitchen", details=details)
    return None


def _order_event_payload(order, actor_id=None, staff_id=None, **extra):
    return {
        "id": order.pk,
        "order_id": order.pk,
        "order_code": order.order_code,
        "table_id": order.table_id,
        "table_label": order.table.label_en if order.table_id else None,
        "status": order.status,
        "actor_id": actor_id,
        "staff_id": staff_id,
        **extra,
    }


def _notify_role_staff(order, event_name, roles, actor_id=None, **extra):
    event_id = uuid.uuid4().hex
    recipients = Staff.objects.filter(branch_id=order.branch_id, role__in=roles, is_active=True).values_list("pk", flat=True)
    for staff_id in recipients:
        publish_staff_event(staff_id, event_name, _order_event_payload(order, actor_id, staff_id, event_id=event_id, **extra))


def _notify_order_staff(order, event_name, actor_id=None, staff_ids=None, **extra):
    recipient_ids = staff_ids if staff_ids is not None else {order.assigned_waiter_id, order.assigned_cashier_id}
    event_id = uuid.uuid4().hex
    for staff_id in recipient_ids - {None}:
        publish_staff_event(staff_id, event_name, _order_event_payload(order, actor_id, staff_id, event_id=event_id, **extra))


def _sync_order_kitchen_status(order, actor):
    order_items = list(order.items.all())
    if any(item.status == OrderItemStatus.PENDING for item in order_items):
        return
    kitchen_items = [item for item in order_items if item.status in {
        OrderItemStatus.FIRED, OrderItemStatus.PREPARING, OrderItemStatus.READY, OrderItemStatus.SERVED
    }]
    if not kitchen_items:
        return
    if all(item.status == OrderItemStatus.SERVED for item in kitchen_items):
        next_status = OrderStatus.SERVED
    elif all(item.status in (OrderItemStatus.READY, OrderItemStatus.SERVED) for item in kitchen_items):
        next_status = OrderStatus.READY
    elif any(item.status == OrderItemStatus.PREPARING for item in kitchen_items):
        next_status = OrderStatus.PREPARING
    else:
        next_status = OrderStatus.SENT
    previous = order.status
    if next_status == previous:
        return
    if _transition_or_conflict(order, next_status, actor, "kitchen_status_changed"):
        return
    return


class OrderListCreateView(ListModelMixin, GenericAPIView):
    """
    GET lists orders for the requester's branch, filterable by ?status=,
    ?table=, and ?from=/?to= (date range on opened_at, YYYY-MM-DD) — added
    ahead of Phase 11 since reports will read from the same Order data, and
    this was the one order-related list endpoint that was simply missing.

    POST is a bespoke get-or-create (mirrors the frontend's
    getOrCreateOpenOrder: one open, non-closed/cancelled order per table),
    not a plain create — that's why this doesn't just use CreateModelMixin.
    """

    serializer_class = OrderSerializer
    permission_classes = [IsPOSStaff]

    def get_permissions(self):
        # Waiters can open an assigned table to add a round; the table and
        # assignment are checked again in post().
        if self.request.method == "GET":
            return [IsOrderStaff()]
        if getattr(self.request.user, "role", None) == StaffRole.WAITER:
            return [IsWaiterStaff()]
        return super().get_permissions()

    def get_queryset(self):
        qs = _order_queryset().filter(branch=self.request.user.branch)
        if self.request.user.role == StaffRole.WAITER:
            qs = qs.filter(assigned_waiter=self.request.user)

        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        table_param = self.request.query_params.get("table")
        if table_param:
            qs = qs.filter(table_id=table_param)

        customer_param = self.request.query_params.get("customer")
        if customer_param:
            qs = qs.filter(customer_id=customer_param)

        waiter_param = self.request.query_params.get("waiter")
        if waiter_param:
            qs = qs.filter(assigned_waiter_id=waiter_param)
        cashier_param = self.request.query_params.get("cashier")
        if cashier_param:
            qs = qs.filter(assigned_cashier_id=cashier_param)
        search = self.request.query_params.get("search", "").strip()
        if search:
            order_number = search.upper()
            if order_number.startswith("ORD-"):
                order_number = order_number[4:]
            order_number = order_number.lstrip("0")
            if order_number.isdigit():
                qs = qs.filter(pk=int(order_number))
            else:
                qs = qs.none()

        date_from = self.request.query_params.get("from")
        date_to = self.request.query_params.get("to")
        if date_from or date_to:
            try:
                branch_tz = ZoneInfo(self.request.user.branch.timezone)
                start_day = parse_date(date_from) if date_from else None
                end_day = parse_date(date_to) if date_to else None
                if (date_from and start_day is None) or (date_to and end_day is None):
                    return qs.none()
                if start_day:
                    qs = qs.filter(opened_at__gte=datetime.combine(start_day, time.min, tzinfo=branch_tz))
                if end_day:
                    qs = qs.filter(opened_at__lt=datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=branch_tz))
            except (ZoneInfoNotFoundError, ValueError):
                return qs.none()

        return qs.order_by("-opened_at")

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request):
        table_id = request.data.get("table")
        if request.user.role == StaffRole.WAITER:
            if not table_id or not Order.objects.filter(
                table_id=table_id, branch=request.user.branch,
                assigned_waiter=request.user,
            ).exclude(status__in=(OrderStatus.CLOSED, OrderStatus.CANCELLED)).exists():
                return Response({"error": "error.orderNotAssignedToWaiter"}, status=status.HTTP_403_FORBIDDEN)
        

        # select_for_update() on the table row makes this atomic against a
        # double-submit (two rapid clicks, or two devices opening the same
        # table at once) — without it, two near-simultaneous requests could
        # both pass the "no open order yet" check before either commits,
        # creating two open orders for the same table.
        with transaction.atomic():
            if table_id:
                table = get_object_or_404(Table.objects.select_for_update(), pk=table_id, branch=request.user.branch)
                try:
                    order, created = get_or_create_open_order(table, staff=request.user)
                except BillLocked:
                    # The customer has already asked to pay. A cashier must
                    # reopen the bill before anything else goes on it.
                    return _bill_locked_response()
            else:
                order = Order.objects.create(branch=request.user.branch, staff=request.user, source=OrderSource.POS)
                created = True
            if created:
                if request.user.role == StaffRole.WAITER:
                    order.assigned_waiter = request.user
                    order.save(update_fields=["assigned_waiter", "updated_at"])
                record_order_event(order, request.user, "order_created", details={"source": order.source})

        order = _order_queryset().get(pk=order.pk)
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class OrderDetailView(RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = [IsOrderStaff]

    def get_queryset(self):
        qs = _order_queryset().filter(branch=self.request.user.branch)
        if self.request.user.role == StaffRole.WAITER:
            qs = qs.filter(assigned_waiter=self.request.user)
        return qs


class OrderHistoryView(ListAPIView):
    serializer_class = OrderHistorySerializer
    permission_classes = [IsOrderStaff]

    def get_queryset(self):
        order_qs = Order.objects.filter(pk=self.kwargs["pk"], branch=self.request.user.branch)
        if self.request.user.role == StaffRole.WAITER:
            order_qs = order_qs.filter(assigned_waiter=self.request.user)
        get_object_or_404(order_qs)
        return OrderHistory.objects.filter(order_id=self.kwargs["pk"], order__branch=self.request.user.branch).select_related("actor")


class CashierReviewView(APIView):
    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk)
            if order.status != OrderStatus.PENDING_CASHIER:
                return Response({"error": "error.orderNotAwaitingCashier"}, status=status.HTTP_409_CONFLICT)
            if not order.items.exclude(status=OrderItemStatus.VOIDED).exists():
                return Response({"error": "error.orderHasNoItems"}, status=status.HTTP_409_CONFLICT)
            conflict = _transition_or_conflict(order, OrderStatus.AWAITING_WAITER, request.user,
                "cashier_reviewed", extra_fields={"assigned_cashier": request.user})
            if conflict:
                return conflict
        return Response(OrderSerializer(_order_queryset().get(pk=order.pk)).data)


class AssignWaiterView(APIView):
    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        waiter_id = request.data.get("waiter")
        if not waiter_id:
            return Response({"error": "error.waiterRequired"}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk)
            if order.status != OrderStatus.AWAITING_WAITER:
                return Response({"error": "error.orderNotAwaitingWaiter"}, status=status.HTTP_409_CONFLICT)
            waiter = get_object_or_404(Staff, pk=waiter_id, branch=request.user.branch,
                role=StaffRole.WAITER, is_active=True)
            previous_waiter = order.assigned_waiter_id
            order.assigned_waiter = waiter
            order.save(update_fields=["assigned_waiter", "updated_at"])
            record_order_event(order, request.user, "waiter_assigned", details={"from": previous_waiter, "to": waiter.pk})
            _notify_order_staff(order, "order:assigned", request.user.pk, {waiter.pk})
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
        return Response(OrderSerializer(_order_queryset().get(pk=order.pk)).data)


class WaiterConfirmView(APIView):
    permission_classes = [IsWaiterStaff]

    def post(self, request, pk):
        try:
            with transaction.atomic():
                order = get_object_or_404(
                    Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk
                )
                if order.status != OrderStatus.AWAITING_WAITER:
                    return Response({"error": "error.orderNotAwaitingWaiter"}, status=status.HTTP_409_CONFLICT)
                is_manager = request.user.role in (StaffRole.OWNER, StaffRole.MANAGER)
                if not is_manager and order.assigned_waiter_id != request.user.pk:
                    return Response({"error": "error.orderNotAssignedToWaiter"}, status=status.HTTP_403_FORBIDDEN)
                conflict = _transition_or_conflict(
                    order,
                    OrderStatus.CONFIRMED,
                    request.user,
                    "waiter_confirmed",
                    extra_fields={"waiter_confirmed_at": timezone.now(), "waiter_confirmed_by": request.user},
                )
                if conflict:
                    return conflict
                dispatch_error = _dispatch_confirmed_order(order, request.user)
                if dispatch_error:
                    return dispatch_error
                _notify_role_staff(order, "order:kitchen_new", {StaffRole.KITCHEN}, request.user.pk)
                _notify_order_staff(order, "order:confirmed", request.user.pk, {order.assigned_cashier_id} - {None})
                _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
        except ProductStockUnavailable:
            return Response({"error": "error.productUnavailable"}, status=status.HTTP_409_CONFLICT)
        return Response(OrderSerializer(_order_queryset().get(pk=order.pk)).data)


class CancelOrderView(APIView):
    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk
            )
            if order.status in TERMINAL_ORDER_STATUSES:
                return Response({"error": "error.orderAlreadyClosed"}, status=status.HTTP_409_CONFLICT)
            if order.status not in {
                OrderStatus.OPEN, OrderStatus.PENDING_CASHIER, OrderStatus.AWAITING_WAITER, OrderStatus.CONFIRMED
            }:
                return Response({"error": "error.orderAlreadyInKitchen"}, status=status.HTTP_409_CONFLICT)
            kitchen_was_notified = order.items.filter(status__in=(OrderItemStatus.FIRED, OrderItemStatus.PREPARING, OrderItemStatus.READY)).exists()
            reason = str(request.data.get("reason", "")).strip()[:500]
            conflict = _transition_or_conflict(
                order,
                OrderStatus.CANCELLED,
                request.user,
                "order_cancelled",
                details={"reason": reason},
                extra_fields={"cancelled_at": timezone.now(), "cancel_reason": reason},
            )
            if conflict:
                return conflict
            order.items.exclude(status=OrderItemStatus.VOIDED).update(status=OrderItemStatus.VOIDED)
            _notify_order_staff(order, "order:cancelled", request.user.pk)
            if kitchen_was_notified:
                _notify_role_staff(order, "order:cancelled", {StaffRole.KITCHEN}, request.user.pk)
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
        return Response(OrderSerializer(_order_queryset().get(pk=order.pk)).data)


class TableOpenOrderView(APIView):
    permission_classes = [IsPOSStaff]

    def get(self, request, pk):
        table = _get_table_or_404(pk, request.user.branch)
        order = _order_queryset().filter(table=table).exclude(status__in=_TERMINAL_STATUSES).order_by("-opened_at", "-id").first()
        if not order:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
        return Response(OrderSerializer(order).data)


class OrderItemsView(APIView):
    permission_classes = [IsOrderStaff]

    def post(self, request, pk):
        with transaction.atomic():
            table_id = Order.objects.filter(pk=pk, branch=request.user.branch).values_list("table_id", flat=True).first()
            if table_id:
                Table.objects.select_for_update().get(pk=table_id, branch=request.user.branch)
            order = get_object_or_404(Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk)
            guard = _closed_order_response(order)
            if guard:
                return guard
            # Adding a line to an existing order is as much "new food on this
            # bill" as opening a new order is, so it is blocked the same way.
            if _locked_bill_for_order(order, actor=request.user):
                return _bill_locked_response()
            if request.user.role == StaffRole.WAITER and order.assigned_waiter_id != request.user.pk:
                return Response({"error": "error.orderNotAssignedToWaiter"}, status=status.HTTP_403_FORBIDDEN)
            serializer = AddOrderItemSerializer(data=request.data, context={"order": order})
            serializer.is_valid(raise_exception=True)
            item = serializer.save()
            if order.table_id:
                order.table.open()
            details = {"item_id": item.pk, "product_id": item.product_id, "quantity": item.quantity}
            if request.user.role == StaffRole.WAITER:
                if order.status == OrderStatus.OPEN:
                    queue_order_for_cashier(order, request.user, "waiter_order_created", details=details)
                else:
                    record_order_event(order, request.user, "waiter_item_added", details=details)
                    _notify_order_staff(order, "order:changed", request.user.pk, {order.assigned_cashier_id} - {None}, change_id=item.pk)
                    _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
            elif order.status in (OrderStatus.SENT, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED):
                record_order_event(order, request.user, "cashier_item_added", details=details)
                _notify_order_staff(order, "order:changed", request.user.pk, {order.assigned_waiter_id} - {None}, change_id=item.pk)
            else:
                queue_order_for_cashier(order, request.user, "item_added", details=details)
        return Response(OrderItemSerializer(item).data, status=status.HTTP_201_CREATED)


class OrderItemDetailView(APIView):
    """Quantity <= 0 removes the item (soft-delete), matching the frontend's
    updateItemQuantity. Snapshot fields and status are never editable here."""

    permission_classes = [IsOrderStaff]

    def patch(self, request, pk, item_id):
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk)
            return self._patch_locked(request, order, item_id)

    def _patch_locked(self, request, order, item_id):
        guard = _closed_order_response(order)
        if guard:
            return guard
        if request.user.role == StaffRole.WAITER and (
            order.assigned_waiter_id != request.user.pk or order.status != OrderStatus.AWAITING_WAITER
        ):
            return Response({"error": "error.orderNotAssignedToWaiter"}, status=status.HTTP_403_FORBIDDEN)

        item = get_object_or_404(OrderItem.objects.select_for_update(), pk=item_id, order=order)
        if item.status != OrderItemStatus.PENDING:
            return Response({"error": "error.itemAlreadyInKitchen"}, status=status.HTTP_409_CONFLICT)

        before = {"quantity": item.quantity, "notes": item.notes}

        quantity = request.data.get("quantity")
        if quantity is not None:
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                return Response({"error": "error.quantityInvalid"}, status=status.HTTP_400_BAD_REQUEST)
            if quantity <= 0:
                details = {"item_id": item.pk, "product_id": item.product_id, **before}
                item.delete()
                if request.user.role == StaffRole.WAITER:
                    record_order_event(order, request.user, "waiter_item_removed", details=details)
                    _notify_order_staff(order, "order:changed", request.user.pk, {order.assigned_cashier_id} - {None}, change_id=item_id)
                    _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
                else:
                    record_order_event(order, request.user, "item_removed", details=details)
                    queue_order_for_cashier(order, request.user, "order_revised")
                return Response(status=status.HTTP_204_NO_CONTENT)
            available, _reason = check_product_stock(item.product, quantity)
            if not is_available_today(item.product):
                available = False
            if not available:
                return Response({"error": "error.productUnavailable"}, status=status.HTTP_409_CONFLICT)
            item.quantity = quantity

        notes = request.data.get("notes")
        if notes is not None:
            item.notes = notes

        item.save()
        details = {
            "item_id": item.pk,
            "product_id": item.product_id,
            "before": before,
            "after": {"quantity": item.quantity, "notes": item.notes},
        }
        if request.user.role == StaffRole.WAITER:
            record_order_event(order, request.user, "waiter_item_updated", details=details)
            _notify_order_staff(order, "order:changed", request.user.pk, {order.assigned_cashier_id} - {None}, change_id=item.pk)
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
        else:
            record_order_event(order, request.user, "item_updated", details=details)
            queue_order_for_cashier(order, request.user, "order_revised")
        return Response(OrderItemSerializer(item).data)


class SendToKitchenView(APIView):
    """
    Flips pending items to fired and deducts recipe stock for each. Wrapped
    in one transaction with select_for_update() on both the pending items
    (so a double-submit of this same request can't double-deduct) and, deeper
    inside adjust_stock(), on each InventoryItem row (so two different
    orders needing the same ingredient can't both read stale stock and
    oversell). See docs/backend-plan.md's Risks section — this is the one
    the plan called out by name.
    """

    permission_classes = [IsOrderStaff]

    def post(self, request, pk):
        order = _get_order_or_404(pk, request.user.branch)
        is_additional_batch = order.status in (
            OrderStatus.SENT, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED,
        )
        if not is_additional_batch and request.user.role not in (
            StaffRole.OWNER, StaffRole.MANAGER, StaffRole.WAITER,
        ):
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)
        if request.user.role == StaffRole.WAITER and order.assigned_waiter_id != request.user.pk:
            return Response({"error": "error.orderNotAssignedToWaiter"}, status=status.HTTP_403_FORBIDDEN)
        if not is_additional_batch and order.status != OrderStatus.CONFIRMED:
            return Response({"error": "error.waiterConfirmationRequired"}, status=status.HTTP_409_CONFLICT)
        if is_additional_batch and request.user.role not in (StaffRole.WAITER, StaffRole.CASHIER, StaffRole.OWNER, StaffRole.MANAGER):
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)
        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order.pk, branch=request.user.branch)
                if is_additional_batch and order.status not in (
                    OrderStatus.SENT, OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.SERVED,
                ):
                    return Response({"error": "error.orderTransitionInvalid"}, status=status.HTTP_409_CONFLICT)
                dispatch_error = _dispatch_confirmed_order(
                    order, request.user, move_order_to_sent=not is_additional_batch,
                )
                if dispatch_error:
                    return dispatch_error
                if is_additional_batch and order.status in (OrderStatus.READY, OrderStatus.SERVED):
                    transition_order(order, OrderStatus.PREPARING, request.user, "additional_items_started")
                if is_additional_batch:
                    _notify_role_staff(order, "order:kitchen_new", {StaffRole.KITCHEN}, request.user.pk)
                    _notify_order_staff(order, "order:changed", request.user.pk)
        except ProductStockUnavailable:
            return Response({"error": "error.productUnavailable"}, status=status.HTTP_409_CONFLICT)

        # _get_order_or_404 prefetched order.items before the bulk .update()
        # above ran, so that cache is now stale — re-fetch fresh rather than
        # serialize `order` directly, or the response would still show the
        # items as "pending" even though the DB is correct.
        order = _get_order_or_404(pk, request.user.branch)
        return Response(OrderSerializer(order).data)


class OrderPaymentsView(APIView):
    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        idempotency_key = str(request.data.get("request_id") or request.headers.get("Idempotency-Key", "")).strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return Response({"error": "error.idempotencyKeyRequired"}, status=status.HTTP_400_BAD_REQUEST)
        serializer = RecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            order = get_object_or_404(
                Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk
            )
            existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                same_request = (
                    existing.order_id == order.pk
                    and existing.amount == serializer.validated_data["amount"]
                    and existing.method == serializer.validated_data["method"]
                    and existing.tip == serializer.validated_data.get("tip")
                )
                if not same_request:
                    return Response({"error": "error.idempotencyKeyConflict"}, status=status.HTTP_409_CONFLICT)
                return Response(PaymentSerializer(existing).data, status=status.HTTP_200_OK)
            if order.status != OrderStatus.SERVED:
                return Response({"error": "error.orderNotServed"}, status=status.HTTP_409_CONFLICT)
            amount = serializer.validated_data["amount"]
            paid_before = sum(
                (payment.amount for payment in order.payments.all()), Decimal("0.00")
            )
            total = _order_total(order)
            if amount > total - paid_before:
                return Response({"error": "error.paymentExceedsBalance"}, status=status.HTTP_400_BAD_REQUEST)
            payment = Payment.objects.create(order=order, idempotency_key=idempotency_key, **serializer.validated_data)
            paid_after = paid_before + amount
            payment_status = PaymentStatus.PAID if paid_after >= total else PaymentStatus.PARTIAL
            if payment_status == PaymentStatus.PAID:
                conflict = _transition_or_conflict(
                    order,
                    OrderStatus.PAID,
                    request.user,
                    "payment_completed",
                    details={"payment_id": payment.pk, "paid_total": str(paid_after), "order_total": str(total), "tip": str(payment.tip or Decimal("0.00"))},
                    extra_fields={"payment_status": payment_status},
                )
                if conflict:
                    return conflict
            else:
                _set_payment_status(
                    order,
                    payment_status,
                    request.user,
                    "payment_recorded",
                    {"payment_id": payment.pk, "paid_total": str(paid_after), "order_total": str(total), "tip": str(payment.tip or Decimal("0.00"))},
                )
            _notify_order_staff(order, "order:payment", request.user.pk, {order.assigned_cashier_id} - {None}, tip=str(payment.tip or Decimal("0.00")))
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk, payment_id=payment.pk)
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


class CloseOrderView(APIView):
    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        order = _get_order_or_404(pk, request.user.branch)
        if order.status != OrderStatus.PAID:
            return Response({"error": "error.orderMustBePaid"}, status=status.HTTP_409_CONFLICT)
        conflict = _transition_or_conflict(
            order,
            OrderStatus.CLOSED,
            request.user,
            "order_closed",
            extra_fields={"closed_at": timezone.now()},
        )
        if conflict:
            return conflict
        return Response(OrderSerializer(order).data)


class KitchenTicketsView(ListAPIView):
    """Orders with at least one item still needing kitchen attention. Order-
    level status (open/sent/closed/...) stays POS's concern — this doesn't
    touch it, only item-level status via OrderItemStatusView below."""

    serializer_class = KitchenTicketOrderSerializer
    permission_classes = [IsKitchenStaff]

    def get_queryset(self):
        active = (OrderItemStatus.FIRED, OrderItemStatus.PREPARING, OrderItemStatus.READY)
        return _order_queryset().filter(branch=self.request.user.branch, items__status__in=active).distinct()


_KDS_ITEM_TRANSITIONS = {
    OrderItemStatus.FIRED: {OrderItemStatus.PREPARING, OrderItemStatus.VOIDED},
    OrderItemStatus.PREPARING: {OrderItemStatus.READY, OrderItemStatus.VOIDED},
    OrderItemStatus.READY: {OrderItemStatus.SERVED, OrderItemStatus.VOIDED},
}


class OrderItemStatusView(APIView):
    """Kitchen progresses an already-fired item forward (preparing/ready/
    served) or voids it. Can't touch an item that's still pending (never
    fired) or already at a terminal status (served/voided)."""

    permission_classes = [IsKitchenStaff]

    def get_permissions(self):
        # Waiters may mark only their assigned, ready items served; kitchen
        # staff retain the fired -> preparing -> ready workflow.
        if getattr(self.request.user, "role", None) == StaffRole.WAITER:
            return [IsWaiterStaff()]
        return [IsKitchenStaff()]

    def patch(self, request, pk, item_id):
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk)
            item = get_object_or_404(OrderItem.objects.select_for_update(), pk=item_id, order=order)
            return self._patch_locked(request, order, item)

    def _patch_locked(self, request, order, item):

        new_status = request.data.get("status")
        if new_status not in {value for values in _KDS_ITEM_TRANSITIONS.values() for value in values}:
            return Response({"error": "error.statusInvalid"}, status=status.HTTP_400_BAD_REQUEST)
        if request.user.role == StaffRole.WAITER:
            if new_status != OrderItemStatus.SERVED or order.assigned_waiter_id != request.user.pk:
                return Response({"error": "error.orderNotAssignedToWaiter"}, status=status.HTTP_403_FORBIDDEN)
            if order.status != OrderStatus.READY:
                return Response({"error": "error.orderNotReady"}, status=status.HTTP_409_CONFLICT)
        elif request.user.role == StaffRole.KITCHEN and new_status == OrderItemStatus.SERVED:
            return Response({"error": "error.statusInvalid"}, status=status.HTTP_403_FORBIDDEN)
        if item.status == OrderItemStatus.PENDING:
            return Response({"error": "error.itemNotFired"}, status=status.HTTP_409_CONFLICT)
        if item.status not in _KDS_ITEM_TRANSITIONS or new_status not in _KDS_ITEM_TRANSITIONS[item.status]:
            return Response({"error": "error.itemAlreadyFinal"}, status=status.HTTP_409_CONFLICT)

        previous_order_status = order.status
        previous_item_status = item.status
        item.status = new_status
        update_fields = ["status", "updated_at"]
        if new_status == OrderItemStatus.READY and item.ready_at is None:
            item.ready_at = timezone.now()
            update_fields.append("ready_at")
        item.save(update_fields=update_fields)
        record_order_event(
            order,
            request.user,
            "kitchen_item_status_changed",
            details={"item_id": item.pk, "from": previous_item_status, "to": new_status},
        )
        getattr(order, "_prefetched_objects_cache", {}).pop("items", None)
        _sync_order_kitchen_status(order, request.user)
        _notify_role_staff(order, "order:changed", {StaffRole.KITCHEN}, request.user.pk, change_id=item.pk, item_status=new_status)
        if (new_status == OrderItemStatus.PREPARING and previous_order_status != OrderStatus.PREPARING
                and order.status == OrderStatus.PREPARING):
            _notify_order_staff(order, "order:preparing", request.user.pk)
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
        elif new_status == OrderItemStatus.READY and previous_order_status != OrderStatus.READY and order.status == OrderStatus.READY:
            _notify_order_staff(order, "order:ready", request.user.pk, {order.assigned_waiter_id} - {None})
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, request.user.pk)
        return Response(OrderItemSerializer(item).data)


class OrderCustomerView(APIView):
    """
    Links (or clears, with `{"customer": null}`) the guest on an order — the
    POS payment screen's optional "add a phone number" step.

    It needs its own endpoint because OrderSerializer is entirely read-only and
    POST /api/orders/ is a get-or-create keyed on the table, so there is no
    other way to put a customer onto an order that already exists. Phase 13's
    public QR flow is what will normally set this at creation time.
    """

    permission_classes = [IsPOSStaff]

    def post(self, request, pk):
        order = _get_order_or_404(pk, request.user.branch)
        guard = _closed_order_response(order)
        if guard:
            return guard

        customer_id = request.data.get("customer")
        if customer_id in (None, ""):
            order.customer = None
        else:
            customer = get_object_or_404(Customer, pk=customer_id, branch=request.user.branch)
            order.customer = customer
            # The same timestamp the field name promises; a customer's history
            # is read from orders anyway, this is just the cheap "when did we
            # last see them" column.
            customer.last_order_at = timezone.now()
            customer.save(update_fields=["last_order_at", "updated_at"])

        order.save(update_fields=["customer", "updated_at"])
        return Response(OrderSerializer(order).data)


class PublicOrderCreateView(APIView):
    """
    Public (AllowAny) — a customer's phone submitting its cart, scoped either
    to a scanned table token or to the public website's default/selected branch.
    One call does what OrderListCreateView.post +
    OrderItemsView.post + OrderCustomerView.post separately do for staff,
    since "submit my order" is a single customer action, not three requests.
    Also doubles as "add more items to an already-open QR order" — calling
    this again for the same table just appends to the existing order rather
    than erroring, since a customer might order a starter, then dessert later.
    """

    permission_classes = [AllowAny]
    require_table = False

    def post(self, request):
        # Accepts the raw "/t/<token>" QR payload as well as a bare token,
        # via the same helper TableBySessionView uses — the phone can post
        # exactly what it scanned or had typed in, unnormalised.
        raw_token = request.data.get("session_token")
        token = parse_qr_payload(raw_token)
        phone = str(request.data.get("phone", "")).strip()
        items_data = request.data.get("items")
        if raw_token and not token:
            return Response({"error": "error.invalidSessionToken"}, status=status.HTTP_400_BAD_REQUEST)
        if self.require_table and not token:
            return Response({"error": "error.sessionTokenRequired"}, status=status.HTTP_400_BAD_REQUEST)
        branch = None
        table = None
        if token:
            table = get_object_or_404(Table, session_token=token)
            branch = table.branch
        else:
            from branches.models import Branch
            branch_id = request.data.get("branch_id")
            branch = get_object_or_404(Branch, pk=branch_id) if branch_id else Branch.objects.order_by("id").first()
            if branch is None:
                return Response({"error": "error.noBranch"}, status=status.HTTP_404_NOT_FOUND)
        if not phone:
            return Response({"error": "error.phoneRequired"}, status=status.HTTP_400_BAD_REQUEST)
        if not items_data:
            return Response({"error": "error.itemsRequired"}, status=status.HTTP_400_BAD_REQUEST)
        idempotency_key = str(request.data.get("request_id") or request.headers.get("Idempotency-Key", "")).strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return Response({"error": "error.idempotencyKeyRequired"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if table:
                table = Table.objects.select_for_update().get(pk=table.pk)
            if table:
                location_payload = request.data.copy()
                validate_branch_location(table.branch, location_payload)
            request_hash = hashlib.sha256(json.dumps({
                "branch": branch.pk,
                "table": table.pk if table else None,
                "phone": phone,
                "name": str(request.data.get("name", "")),
                "items": items_data,
            }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
            previous = CustomerOrderSubmission.objects.select_for_update().filter(idempotency_key=idempotency_key).first()
            if previous:
                if previous.request_hash != request_hash:
                    return Response({"error": "error.idempotencyKeyConflict"}, status=status.HTTP_409_CONFLICT)
                order = previous.order
                return Response(PublicOrderSerializer(order).data, status=status.HTTP_200_OK)

            customer, _ = find_or_create_customer(branch, phone, request.data.get("name", ""))
            try:
                if table:
                    # Each customer checkout is a distinct kitchen ticket. This
                    # lets guests add another round while an earlier ticket is
                    # preparing without rewriting or re-queuing that ticket.
                    order, created = get_or_create_open_order(
                        table, customer=customer, source=OrderSource.QR, force_new=True,
                    )
                else:
                    order = Order.objects.create(branch=branch, customer=customer, source=OrderSource.WEB)
                    created = True
            except BillLocked:
                # This table has already asked for the bill. Blocking here is
                # the whole point of pay_requested: the amount the customer is
                # about to pay must not be able to change under them.
                # Checked BEFORE table.open() below, which would otherwise
                # flip the table back from `needs-bill` to `occupied` on a
                # request we are about to refuse (a `return` inside
                # transaction.atomic() commits — it does not roll back).
                return _bill_locked_response()
            if table:
                table.open()  # Preserve the permanent QR and mark occupied only when an order is placed.
            if created:
                record_order_event(order, None, "order_created", details={"source": order.source})
            if order.customer_id is None:
                order.customer = customer
                order.save(update_fields=["customer", "updated_at"])

            added_items = []
            for line in items_data:
                item_serializer = AddOrderItemSerializer(data=line, context={"order": order})
                item_serializer.is_valid(raise_exception=True)
                added_items.append(item_serializer.save())

            queue_order_for_cashier(
                order,
                None,
                "customer_order_placed",
                details={"item_ids": [item.pk for item in added_items]},
            )
            CustomerOrderSubmission.objects.create(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                order=order,
            )
            _notify_role_staff(order, "order:cashier_review", {StaffRole.CASHIER}, None)
            _notify_role_staff(order, "order:admin_update", {StaffRole.OWNER, StaffRole.MANAGER}, None)

            customer.last_order_at = timezone.now()
            customer.save(update_fields=["last_order_at", "updated_at"])

        order = Order.objects.prefetch_related("items").get(pk=order.pk)
        return Response(PublicOrderSerializer(order).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class PublicOrderTrackView(APIView):
    """Public status lookup requiring both the non-sequential order code and phone."""
    permission_classes = [AllowAny]

    def get(self, request):
        code = str(request.query_params.get("order_code", "")).strip()
        phone = str(request.query_params.get("phone", "")).strip()
        try:
            order_id = int(code.removeprefix("ORD-"))
        except ValueError:
            order_id = 0
        order = Order.objects.filter(pk=order_id, customer__phone=phone).prefetch_related("items").first()
        if order and order.order_code != code:
            order = None
        if not order:
            return Response({"error": "error.orderNotFound"}, status=status.HTTP_404_NOT_FOUND)
        return Response(PublicOrderSerializer(order).data)


class PublicQrOrderCreateView(PublicOrderCreateView):
    require_table = True
