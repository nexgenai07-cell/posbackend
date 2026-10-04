from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsOwnerOrManager
from common.mixins import BranchScopedQuerysetMixin
from inventory.models import InventoryItem, StockMovementReason
from inventory.services import adjust_stock
from reports.filters import branch_datetime_bounds, parse_date_range

from .models import Purchase, PurchaseStatus, Supplier
from .serializers import PurchaseSerializer, SupplierSerializer


class SupplierViewSet(viewsets.ModelViewSet):
    """Suppliers aren't branch-scoped — matches the frontend's Supplier type,
    which has no branchId. Read is any authenticated staff; write is
    owner/manager only."""

    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def destroy(self, request, *args, **kwargs):
        supplier = self.get_object()
        if supplier.purchases.exists():
            return Response({"error": "error.supplierInUse"}, status=status.HTTP_409_CONFLICT)
        supplier.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PurchaseViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Read is any authenticated staff; write is owner/manager only. Once a
    purchase is received, it's immutable — no PATCH, no DELETE."""

    queryset = Purchase.objects.select_related("supplier").prefetch_related("items")
    serializer_class = PurchaseSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def update(self, request, *args, **kwargs):
        if self.get_object().status == PurchaseStatus.RECEIVED:
            return Response({"error": "error.purchaseAlreadyReceived"}, status=status.HTTP_409_CONFLICT)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        purchase = self.get_object()
        if purchase.status == PurchaseStatus.RECEIVED:
            return Response({"error": "error.purchaseAlreadyReceived"}, status=status.HTTP_409_CONFLICT)
        purchase.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"], permission_classes=[IsOwnerOrManager], url_path="summary")
    def summary(self, request):
        from .models import PurchaseItem

        from_date, to_date = parse_date_range(request)
        start, end = branch_datetime_bounds(request.user.branch, from_date, to_date)
        status_filter = request.query_params.get("status")
        supplier_filter = request.query_params.get("supplier")
        search = request.query_params.get("search", "").strip()
        base = Purchase.objects.filter(branch=request.user.branch)
        if status_filter in {choice for choice, _label in PurchaseStatus.choices}:
            base = base.filter(status=status_filter)
        if supplier_filter:
            base = base.filter(supplier_id=supplier_filter)
        if search:
            base = base.filter(Q(supplier__name_en__icontains=search) | Q(supplier__name_ar__icontains=search) | Q(pk__icontains=search)).distinct()

        ordered = base.filter(ordered_at__gte=start, ordered_at__lt=end)
        received = base.filter(status=PurchaseStatus.RECEIVED, received_at__gte=start, received_at__lt=end)
        pending = base.exclude(status=PurchaseStatus.RECEIVED).filter(ordered_at__gte=start, ordered_at__lt=end)
        received_items = PurchaseItem.objects.filter(purchase__in=received).select_related("inventory_item", "purchase__supplier")
        pending_items = PurchaseItem.objects.filter(purchase__in=pending)
        spend_by_ingredient = {}
        unit_quantities = {}
        purchase_total = Decimal("0")
        for line in received_items:
            total = line.quantity * line.unit_cost
            purchase_total += total
            key = line.inventory_item_id
            row = spend_by_ingredient.setdefault(key, {
                "inventory_item_id": key,
                "name": line.inventory_item.name,
                "unit": line.inventory_item.unit,
                "quantity": Decimal("0"),
                "spend": Decimal("0"),
            })
            row["quantity"] += line.quantity
            row["spend"] += total
            unit_quantities[line.inventory_item.unit] = unit_quantities.get(line.inventory_item.unit, Decimal("0")) + line.quantity
        pending_total = sum((line.quantity * line.unit_cost for line in pending_items), Decimal("0"))
        top = sorted(spend_by_ingredient.values(), key=lambda row: row["spend"], reverse=True)
        supplier_costs = {}
        for line in received_items:
            supplier_id = line.purchase.supplier_id
            supplier = supplier_costs.setdefault(supplier_id, {
                "supplier_id": supplier_id,
                "supplier_name": line.purchase.supplier.name_en,
                "total": Decimal("0"),
            })
            supplier["total"] += line.quantity * line.unit_cost
        completed_count = received.count()
        return Response({
            "from": from_date,
            "to": to_date,
            "purchase_order_count": ordered.count(),
            "received_order_count": completed_count,
            "total_purchase_cost": purchase_total,
            "average_purchase_value": purchase_total / completed_count if completed_count else Decimal("0"),
            "pending_purchase_value": pending_total,
            "ingredients_purchased_by_unit": unit_quantities,
            "top_purchased_ingredients": top[:10],
            "supplier_spend": list(supplier_costs.values()),
        })

    @action(detail=True, methods=["post"], url_path="receive")
    def receive(self, request, pk=None):
        """
        The only place a PO touches stock: adjust_stock() per line (locked,
        transactional — same protection as send-to-kitchen) plus updating
        InventoryItem.cost_per_unit from what was actually paid. Guarded
        twice against a double-receive race: the outer check below is just
        a fast early-exit, the select_for_update() re-check inside the
        transaction is the actual protection.
        """
        purchase = self.get_object()
        if purchase.status == PurchaseStatus.RECEIVED:
            return Response({"error": "error.purchaseAlreadyReceived"}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
            if purchase.status == PurchaseStatus.RECEIVED:
                return Response({"error": "error.purchaseAlreadyReceived"}, status=status.HTTP_409_CONFLICT)

            for purchase_item in purchase.items.select_related("inventory_item"):
                adjust_stock(
                    purchase_item.inventory_item_id,
                    purchase_item.quantity,
                    StockMovementReason.PURCHASE,
                    purchase=purchase,
                    purchase_item=purchase_item,
                    unit_cost_snapshot=purchase_item.unit_cost,
                )
                InventoryItem.objects.filter(pk=purchase_item.inventory_item_id).update(cost_per_unit=purchase_item.unit_cost)

            purchase.status = PurchaseStatus.RECEIVED
            purchase.received_at = timezone.now()
            purchase.save(update_fields=["status", "received_at", "updated_at"])

        # Re-fetch fresh rather than reuse `purchase` — same prefetch-cache
        # staleness lesson from Phase 8's SendToKitchenView bug.
        purchase = self.get_queryset().get(pk=purchase.pk)
        return Response(PurchaseSerializer(purchase).data)
