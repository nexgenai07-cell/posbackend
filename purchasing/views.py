from decimal import Decimal

from django.db import transaction
from django.http import HttpResponse
from django.db.models import F, OuterRef, Prefetch, Q, Subquery, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsOwnerOrManager
from common.mixins import BranchScopedQuerysetMixin
from inventory.models import InventoryItem, StockMovement, StockMovementReason
from inventory.services import adjust_stock
from reports.filters import branch_datetime_bounds, parse_date_range

from . import vat as purchase_vat
from .models import Purchase, PurchaseItem, PurchaseStatus, Supplier, SupplierDocument, SupplierDocumentKind
from .services import apply_purchase_stock, reverse_purchase_stock
from .serializers import PurchaseSerializer, SupplierDocumentSerializer, SupplierSerializer


class SupplierViewSet(viewsets.ModelViewSet):
    """Suppliers aren't branch-scoped — matches the frontend's Supplier type,
    which has no branchId. Read is any authenticated staff; write is
    owner/manager only."""

    queryset = Supplier.objects.all()
    serializer_class = SupplierSerializer

    def get_queryset(self):
        from common.filters import text_filter
        qs = super().get_queryset()
        params = self.request.query_params
        qs = text_filter(qs, params, "search", ["name_en", "name_ar", "contact_info"])
        has_contact = (params.get("has_contact") or "").strip().lower()
        if has_contact in ("true", "1", "yes"):
            qs = qs.exclude(contact_info="").exclude(contact_info__isnull=True)
        elif has_contact in ("false", "0", "no"):
            qs = qs.filter(contact_info__in=["", None])
        return qs

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

    # Everything the serializer reads per line, loaded up front: the stock
    # item (name, unit) and the line's stock movements (before / added /
    # after). Without these the list ran ~4 queries per purchase line.
    queryset = Purchase.objects.select_related("supplier", "branch").prefetch_related(
        Prefetch("items", queryset=PurchaseItem.objects.select_related("inventory_item").prefetch_related(
            Prefetch("stock_movements", queryset=StockMovement.objects.order_by("id"), to_attr="movements_by_id"),
        )),
        "supplier_documents",
    )
    serializer_class = PurchaseSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def get_queryset(self):
        from common.filters import choice_filter, fk_filter, ordering_filter
        from reports.filters import branch_datetime_bounds, parse_date_range
        qs = super().get_queryset()
        params = self.request.query_params
        qs = fk_filter(qs, params, "supplier", field="supplier_id")
        qs = choice_filter(
            qs, params, "status", field="status",
            allowed=[choice for choice, _ in PurchaseStatus.choices]
        )
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(supplier__name_en__icontains=search) |
                Q(supplier__name_ar__icontains=search) |
                Q(pk__icontains=search)
            ).distinct()
        from_date, to_date = parse_date_range(self.request)
        if from_date or to_date:
            start, end = branch_datetime_bounds(self.request.user.branch, from_date, to_date)
            if from_date:
                qs = qs.filter(ordered_at__gte=start)
            if to_date:
                qs = qs.filter(ordered_at__lt=end)
        # The admin pages this list on the server, so it is sorted here too.
        line_total = Subquery(
            PurchaseItem.objects.filter(purchase=OuterRef("pk")).values("purchase")
            .annotate(total=Sum(F("quantity") * F("unit_cost"))).values("total")[:1]
        )
        return ordering_filter(
            qs, params,
            allowed={
                "supplier": "supplier__name_en", "status": "status", "ordered_at": "ordered_at",
                "received_at": "received_at", "total": line_total,
            },
            default=["-ordered_at"],
        )

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

        # Optional: the supplier's tax invoice, keyed in at the door. Checked
        # before anything moves so a typo can't leave stock half-received.
        invoice = None
        invoice_data = request.data.get("supplier_invoice")
        if invoice_data:
            existing = next((d for d in purchase.supplier_documents.all()
                             if d.kind == SupplierDocumentKind.INVOICE), None)
            invoice = SupplierDocumentSerializer(existing, data=invoice_data, partial=existing is not None,
                                                 context={"purchase": purchase})
            if not invoice.is_valid():
                return Response({"supplier_invoice": invoice.errors}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            purchase = Purchase.objects.select_for_update().get(pk=purchase.pk)
            if purchase.status == PurchaseStatus.RECEIVED:
                return Response({"error": "error.purchaseAlreadyReceived"}, status=status.HTTP_409_CONFLICT)

            # Shared with the re-apply step of update() above, so receiving and
            # re-receiving-after-an-edit can never drift apart.
            apply_purchase_stock(purchase, actor=request.user)
            if invoice is not None:
                invoice.save(purchase=purchase, kind=SupplierDocumentKind.INVOICE,
                             **({} if invoice.instance else {"created_by": request.user}))

            purchase.status = PurchaseStatus.RECEIVED
            purchase.received_at = timezone.now()
            purchase.save(update_fields=["status", "received_at", "updated_at"])

        # Re-fetch fresh rather than reuse `purchase` — same prefetch-cache
        # staleness lesson from Phase 8's SendToKitchenView bug.
        purchase = self.get_queryset().get(pk=purchase.pk)
        return Response(PurchaseSerializer(purchase).data)

    # -- supplier tax documents (input VAT) ------------------------------------
    # Recorded, checked and reported here; never sent to ZATCA — the supplier
    # reports its own invoices and credit notes.

    def _purchase_documents(self, purchase, kind):
        return SupplierDocument.objects.filter(purchase=purchase, kind=kind)

    @action(detail=True, methods=["get", "put", "delete"], url_path="supplier-invoice")
    def supplier_invoice(self, request, pk=None):
        """The supplier's tax invoice for this purchase (at most one).
        PUT creates or replaces it; the QR is re-checked on every save."""
        purchase = self.get_object()
        invoice = self._purchase_documents(purchase, SupplierDocumentKind.INVOICE).first()
        if request.method == "GET":
            if invoice is None:
                return Response(None)
            return Response(SupplierDocumentSerializer(invoice, context={"purchase": purchase}).data)
        if request.method == "DELETE":
            if invoice is not None:
                invoice.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = SupplierDocumentSerializer(invoice, data=request.data, context={"purchase": purchase})
        serializer.is_valid(raise_exception=True)
        extra = {} if invoice else {"purchase": purchase, "kind": SupplierDocumentKind.INVOICE,
                                    "created_by": request.user}
        document = serializer.save(**extra)
        return Response(SupplierDocumentSerializer(document, context={"purchase": purchase}).data,
                        status=status.HTTP_200_OK if invoice else status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"], url_path="supplier-credit-notes")
    def supplier_credit_notes(self, request, pk=None):
        """The supplier's credit notes for returns on this purchase. POST may
        also take the goods back out of stock: `returned_lines: [{purchase_item,
        quantity}]`, booked as an adjustment linked to this purchase (so a
        later edit of the PO doesn't put them back)."""
        purchase = self.get_object()
        if request.method == "GET":
            notes = self._purchase_documents(purchase, SupplierDocumentKind.CREDIT_NOTE)
            return Response(SupplierDocumentSerializer(notes, many=True, context={"purchase": purchase}).data)

        serializer = SupplierDocumentSerializer(data=request.data, context={"purchase": purchase})
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data.get("reason", "").strip():
            return Response({"reason": ["error.supplierCreditReasonRequired"]}, status=status.HTTP_400_BAD_REQUEST)
        returned = []
        lines = {line.pk: line for line in purchase.items.all()}
        for raw in request.data.get("returned_lines") or []:
            try:
                line, quantity = lines[int(raw["purchase_item"])], Decimal(str(raw["quantity"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return Response({"error": "error.supplierReturnLinesInvalid"}, status=status.HTTP_400_BAD_REQUEST)
            if quantity <= 0:
                continue
            if quantity > line.quantity:
                return Response({"error": "error.supplierReturnTooMuch"}, status=status.HTTP_400_BAD_REQUEST)
            returned.append((line, quantity))
        if returned and purchase.status != PurchaseStatus.RECEIVED:
            return Response({"error": "error.supplierReturnNotReceived"}, status=status.HTTP_409_CONFLICT)

        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            with transaction.atomic():
                note = serializer.save(purchase=purchase, kind=SupplierDocumentKind.CREDIT_NOTE,
                                       created_by=request.user)
                for line, quantity in returned:
                    adjust_stock(line.inventory_item_id, -quantity, StockMovementReason.ADJUSTMENT,
                                 purchase=purchase, purchase_item=line, unit_cost_snapshot=line.unit_cost)
        except DjangoValidationError as exc:
            return Response({"error": exc.messages[0]}, status=status.HTTP_409_CONFLICT)
        return Response(SupplierDocumentSerializer(note, context={"purchase": purchase}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"supplier-credit-notes/(?P<document_id>\d+)")
    def supplier_credit_note(self, request, pk=None, document_id=None):
        """Correct or remove a keyed-in credit note. Removing it doesn't put
        returned goods back: that's a stock adjustment, done on purpose."""
        purchase = self.get_object()
        note = self._purchase_documents(purchase, SupplierDocumentKind.CREDIT_NOTE).filter(pk=document_id).first()
        if note is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
        if request.method == "DELETE":
            note.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = SupplierDocumentSerializer(note, data=request.data, partial=True, context={"purchase": purchase})
        serializer.is_valid(raise_exception=True)
        note = serializer.save()
        return Response(SupplierDocumentSerializer(note, context={"purchase": purchase}).data)

    @action(detail=False, methods=["post"], url_path="qr-check")
    def qr_check(self, request):
        """POST {qr_text, supplier?, issue_date?, total?, vat?}: decode a
        supplier QR without saving anything (the form's "Check QR" button)."""
        supplier_vat = ""
        supplier_id = request.data.get("supplier")
        if supplier_id:
            supplier = Supplier.objects.filter(pk=supplier_id).first()
            supplier_vat = supplier.vat_number if supplier else ""
        issue_date = None
        if request.data.get("issue_date"):
            from django.utils.dateparse import parse_date
            try:
                issue_date = parse_date(str(request.data["issue_date"]))
            except ValueError:
                issue_date = None

        def amount(name):
            value = request.data.get(name)
            if value in (None, ""):
                return None
            try:
                return Decimal(str(value))
            except ArithmeticError:
                return None

        return Response(purchase_vat.check_qr(str(request.data.get("qr_text", "")), supplier_vat=supplier_vat,
                                              issue_date=issue_date, total=amount("total"), vat=amount("vat")))

    @action(detail=False, methods=["get"], url_path="input-vat")
    def input_vat(self, request):
        """
        Input VAT for a period, by the supplier document's date: every
        supplier invoice and credit note (credit notes reduce the VAT), with
        flags for anything a person should look at, plus received purchases
        in the period that have no supplier invoice yet. Purchases from
        suppliers that aren't VAT-registered are listed but claim nothing.
        """
        from_date, to_date = parse_date_range(request)
        branch = request.user.branch
        documents = (SupplierDocument.objects.filter(purchase__branch=branch, issue_date__gte=from_date,
                                                     issue_date__lte=to_date)
                     .select_related("purchase__supplier")
                     .prefetch_related("purchase__items")
                     .order_by("issue_date", "id"))
        zero = Decimal("0.00")
        totals = {"net": zero, "vat": zero, "total": zero, "claimable_vat": zero}
        rows = []
        for document in documents:
            supplier = document.purchase.supplier
            sign = -1 if document.kind == SupplierDocumentKind.CREDIT_NOTE else 1
            flags = purchase_vat.document_flags(document, supplier)
            if not supplier.vat_registered:
                flags.append(purchase_vat.NOT_REGISTERED)
            elif not supplier.vat_number:
                flags.append(purchase_vat.SUPPLIER_VAT)
            if document.kind == SupplierDocumentKind.INVOICE:
                subtotal = sum((line.quantity * line.unit_cost for line in document.purchase.items.all()),
                               Decimal("0"))
                if abs(subtotal - document.net) > Decimal("0.05"):
                    flags.append(purchase_vat.NET_VS_PO)
            claimable = purchase_vat.claimable_vat(document, supplier)
            totals["net"] += sign * document.net
            totals["vat"] += sign * document.vat
            totals["total"] += sign * document.total
            totals["claimable_vat"] += claimable
            rows.append({
                "id": document.pk, "kind": document.kind, "purchase": document.purchase_id,
                "supplier": supplier.pk, "supplier_name_en": supplier.name_en, "supplier_name_ar": supplier.name_ar,
                "supplier_vat_number": supplier.vat_number, "vat_registered": supplier.vat_registered,
                "number": document.number, "issue_date": document.issue_date,
                "net": sign * document.net, "vat": sign * document.vat, "total": sign * document.total,
                "claimable_vat": claimable, "flags": flags,
            })

        start, end = branch_datetime_bounds(branch, from_date, to_date)
        missing = []
        for purchase in (Purchase.objects.filter(branch=branch, status=PurchaseStatus.RECEIVED,
                                                 received_at__gte=start, received_at__lt=end)
                         .exclude(supplier_documents__kind=SupplierDocumentKind.INVOICE)
                         .select_related("supplier").prefetch_related("items").order_by("received_at")):
            missing.append({
                "purchase": purchase.pk, "supplier": purchase.supplier_id,
                "supplier_name_en": purchase.supplier.name_en, "supplier_name_ar": purchase.supplier.name_ar,
                "vat_registered": purchase.supplier.vat_registered, "received_at": purchase.received_at,
                "subtotal": sum((line.quantity * line.unit_cost for line in purchase.items.all()), Decimal("0")),
                "flags": [purchase_vat.NO_INVOICE] if purchase.supplier.vat_registered
                else [purchase_vat.NO_INVOICE, purchase_vat.NOT_REGISTERED],
            })
        return Response({"from": from_date, "to": to_date, "rows": rows, "missing_invoices": missing,
                         "totals": totals})
