from decimal import Decimal

from django.db import transaction
from django.http import HttpResponse
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
from .services import apply_purchase_stock, reverse_purchase_stock
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
        """
        Editing a RECEIVED purchase is allowed (owner/manager), and reconciles
        inventory rather than leaving it stale.

        Received POs take the reverse -> edit -> re-apply path, all inside one
        transaction: if any step fails, stock is left exactly as it was rather
        than half-adjusted. Changing 20 KG to 15 KG therefore leaves +15 in
        stock, not the +20 the original receive added.
        """
        purchase = self.get_object()
        if purchase.status != PurchaseStatus.RECEIVED:
            return super().update(request, *args, **kwargs)

        with transaction.atomic():
            locked = Purchase.objects.select_for_update().get(pk=purchase.pk)
            reverse_purchase_stock(locked, actor=request.user)
            response = super().update(request, *args, **kwargs)
            if response.status_code >= 400:
                # Roll the reversal back with the failed edit — a rejected
                # payload must not quietly empty the shelves.
                transaction.set_rollback(True)
                return response
            locked.refresh_from_db()
            apply_purchase_stock(
                Purchase.objects.prefetch_related("items__inventory_item").get(pk=locked.pk),
                actor=request.user,
            )
        return response

    def destroy(self, request, *args, **kwargs):
        """
        Deleting a RECEIVED purchase first takes its stock back out.

        Dropping the row while leaving the inventory it added would overstate
        stock permanently, with nothing in the ledger to explain the gap.
        """
        purchase = self.get_object()
        if purchase.status != PurchaseStatus.RECEIVED:
            purchase.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        with transaction.atomic():
            locked = Purchase.objects.select_for_update().get(pk=purchase.pk)
            reverse_purchase_stock(locked, actor=request.user)
            # Soft delete (BaseModel default): the StockMovement rows FK to this
            # purchase and the ledger must stay readable for historical reports.
            locked.delete()
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

    @action(detail=True, methods=["post"], url_path="set-status", permission_classes=[IsOwnerOrManager])
    def set_status(self, request, pk=None):
        """
        POST /api/purchases/<id>/set-status/  {"status": "draft|ordered|received"}

        Change a purchase order's status at ANY time, including moving it back
        out of `received` — a PO marked received by mistake has to be
        correctable without deleting it and re-keying every line.

        Stock follows the status, which is the whole point:
          -> received      adds the lines to stock (same path as receive())
          received ->      takes them back out again
          neither          no stock movement, just a label change

        All inside one transaction, so a failure leaves both the status and the
        shelves exactly as they were rather than half-applied.
        """
        new_status = str(request.data.get("status", "")).strip()
        valid = {choice for choice, _label in PurchaseStatus.choices}
        if new_status not in valid:
            return Response({"error": "error.purchaseStatusInvalid"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            purchase = Purchase.objects.select_for_update().get(
                pk=self.get_object().pk, branch=request.user.branch,
            )
            was_received = purchase.status == PurchaseStatus.RECEIVED
            now_received = new_status == PurchaseStatus.RECEIVED
            if was_received == now_received:
                # Same side of the received line: nothing to reconcile.
                purchase.status = new_status
                purchase.save(update_fields=["status", "updated_at"])
            elif now_received:
                apply_purchase_stock(
                    Purchase.objects.prefetch_related("items__inventory_item").get(pk=purchase.pk),
                    actor=request.user,
                )
                purchase.status = new_status
                purchase.received_at = timezone.now()
                purchase.save(update_fields=["status", "received_at", "updated_at"])
            else:
                reverse_purchase_stock(purchase, actor=request.user)
                purchase.status = new_status
                # Clear the timestamp too, or the PO claims a receipt date it
                # no longer has.
                purchase.received_at = None
                purchase.save(update_fields=["status", "received_at", "updated_at"])

        return Response(PurchaseSerializer(self.get_queryset().get(pk=purchase.pk)).data)

    @action(detail=True, methods=["get"], url_path="pdf")
    def pdf(self, request, pk=None):
        """
        GET /api/purchases/<id>/pdf/ — the purchase order as a PDF.

        Built server-side from the database so the PDF, the print view and the
        on-screen PO can never disagree: all three read the same rows. A
        browser-side generator could render whatever a stale tab was holding.

        Branding is the configured Branch name/logo, not a hard-coded
        restaurant.
        """
        purchase = self.get_object()
        try:
            from .pdf import build_purchase_pdf
        except ImportError:
            # reportlab missing -> a clear, actionable code instead of a 500.
            return Response(
                {"error": "error.pdfNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        pdf_bytes = build_purchase_pdf(purchase)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="PO-{purchase.pk}.pdf"'
        return response

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

            # Shared with the re-apply step of update() above, so receiving and
            # re-receiving-after-an-edit can never drift apart.
            apply_purchase_stock(purchase, actor=request.user)

            purchase.status = PurchaseStatus.RECEIVED
            purchase.received_at = timezone.now()
            purchase.save(update_fields=["status", "received_at", "updated_at"])

        # Re-fetch fresh rather than reuse `purchase` — same prefetch-cache
        # staleness lesson from Phase 8's SendToKitchenView bug.
        purchase = self.get_queryset().get(pk=purchase.pk)
        return Response(PurchaseSerializer(purchase).data)
