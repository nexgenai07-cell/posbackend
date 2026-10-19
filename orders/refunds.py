"""
Partial refunds: money back for some lines (or part of a line's quantity)
of a paid order that otherwise stands. Cancelling the whole order is
CancelOrderView; this is the "Refund items" action next to it.

Owners and managers only, with a reason, because it gives money back.
When the order was tax-invoiced, the refund gets its own ZATCA credit note
for exactly those lines, issued after commit (zatca/pos.py) and backed by
the retry job like every other document — so ZATCA can never stop a refund.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager

from .models import Order, OrderItemStatus, OrderRefund, OrderRefundLine, OrderStatus, PaymentMethod, PaymentStatus
from .workflow import record_order_event


def _refunded_quantities(order):
    return dict(OrderRefundLine.objects.filter(refund__order=order).values("item_id")
                .annotate(total=Sum("quantity")).values_list("item_id", "total"))


def _refundable(order):
    refunded = _refunded_quantities(order)
    lines = []
    for item in order.items.exclude(status=OrderItemStatus.VOIDED).order_by("pk"):
        done = refunded.get(item.pk, 0)
        lines.append({
            "item": item.pk, "name_en": item.name_en_snapshot, "name_ar": item.name_ar_snapshot,
            "variant_en": item.variant_name_en_snapshot, "variant_ar": item.variant_name_ar_snapshot,
            "quantity": item.quantity, "refunded": done, "refundable": max(item.quantity - done, 0),
            "unit_price": item.price_snapshot,
        })
    return lines


def _summary(order):
    paid = sum((payment.amount for payment in order.payments.all()), Decimal("0"))
    refunds = list(order.refunds.prefetch_related("lines").order_by("created_at", "pk"))
    return {
        "allowed": order.payment_status == PaymentStatus.PAID and order.status != OrderStatus.CANCELLED,
        "paid": paid,
        "refunded": order.refunded_amount or Decimal("0"),
        "lines": _refundable(order),
        "refunds": [{
            "id": refund.pk, "amount": refund.amount, "method": refund.method, "reason": refund.reason,
            "created_at": refund.created_at, "refunded_by": refund.refunded_by_id,
            "lines": [{"item": line.item_id, "quantity": line.quantity, "amount": line.amount}
                      for line in refund.lines.all()],
        } for refund in refunds],
    }


class OrderRefundView(APIView):
    """GET: what can still be refunded. POST {lines: [{item, quantity}],
    method, reason}: refund those quantities at the price charged."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        order = get_object_or_404(Order.objects.filter(branch=request.user.branch), pk=pk)
        return Response(_summary(order))

    def post(self, request, pk):
        reason = str(request.data.get("reason", "")).strip()[:255]
        method = str(request.data.get("method", "")).strip()
        raw_lines = request.data.get("lines")
        if not reason:
            return Response({"error": "error.refundReasonRequired"}, status=status.HTTP_400_BAD_REQUEST)
        if method not in PaymentMethod.values:
            return Response({"error": "error.refundMethodInvalid"}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(raw_lines, list) or not raw_lines:
            return Response({"error": "error.refundLinesRequired"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update().filter(branch=request.user.branch), pk=pk)
            if order.status == OrderStatus.CANCELLED or order.payment_status != PaymentStatus.PAID:
                return Response({"error": "error.refundNotPaid"}, status=status.HTTP_409_CONFLICT)
            refundable = {line["item"]: line for line in _refundable(order)}
            wanted = {}
            for raw in raw_lines:
                try:
                    item_id, quantity = int(raw["item"]), int(raw["quantity"])
                except (KeyError, TypeError, ValueError):
                    return Response({"error": "error.refundLinesInvalid"}, status=status.HTTP_400_BAD_REQUEST)
                if quantity <= 0:
                    continue
                wanted[item_id] = wanted.get(item_id, 0) + quantity
            if not wanted:
                return Response({"error": "error.refundLinesRequired"}, status=status.HTTP_400_BAD_REQUEST)
            for item_id, quantity in wanted.items():
                if item_id not in refundable or quantity > refundable[item_id]["refundable"]:
                    return Response({"error": "error.refundQuantityTooHigh"}, status=status.HTTP_400_BAD_REQUEST)

            amount = sum((refundable[item_id]["unit_price"] * quantity for item_id, quantity in wanted.items()),
                         Decimal("0"))
            summary_paid = sum((payment.amount for payment in order.payments.all()), Decimal("0"))
            already = order.refunded_amount or Decimal("0")
            if already + amount > summary_paid:
                return Response({"error": "error.refundMoreThanPaid"}, status=status.HTTP_400_BAD_REQUEST)

            refund = OrderRefund.objects.create(order=order, amount=amount, method=method, reason=reason,
                                                refunded_by=request.user)
            OrderRefundLine.objects.bulk_create([
                OrderRefundLine(refund=refund, item_id=item_id, quantity=quantity,
                                unit_price=refundable[item_id]["unit_price"],
                                amount=refundable[item_id]["unit_price"] * quantity)
                for item_id, quantity in wanted.items()
            ])
            order.refunded_amount = already + amount
            order.save(update_fields=["refunded_amount", "updated_at"])
            record_order_event(order, request.user, "order_partially_refunded", details={
                "refund_id": refund.pk, "amount": str(amount), "method": method, "reason": reason,
                "lines": [{"item": item_id, "quantity": quantity} for item_id, quantity in wanted.items()],
            })
            from zatca import pos as zatca_pos
            zatca_pos.order_refunded(refund.pk)

        return Response(_summary(Order.objects.get(pk=order.pk)), status=status.HTTP_201_CREATED)
