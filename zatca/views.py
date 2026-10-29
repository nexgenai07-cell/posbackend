from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsCashierStaff, IsOwner, IsOwnerOrManager

from . import crypto, golive, pos, receipts, ubl
from .environments import profile
from .models import (
    BusinessCustomer,
    DocumentStatus,
    GoLiveStatus,
    InvoiceTypes,
    ZatcaDevice,
    ZatcaDocument,
    ZatcaDocumentOrder,
    ZatcaGoLive,
    ZatcaOrderBuyer,
    ZatcaSettings,
)
from .serializers import (
    BusinessCustomerSerializer,
    ZatcaDeviceSerializer,
    ZatcaDocumentSerializer,
    ZatcaSettingsSerializer,
)


class ZatcaSettingsView(APIView):
    """
    The requesting staff's own branch's ZATCA business details. Owners and
    managers can read them; only an owner can change them, because they are
    what every tax invoice says about the seller.

    The first GET creates the row pre-filled with the sandbox test identity,
    so a fresh install shows a working form rather than an empty one.
    """

    def get_permissions(self):
        return [IsOwner()] if self.request.method == "PATCH" else [IsOwnerOrManager()]

    def get(self, request):
        settings = ZatcaSettings.for_branch(request.user.branch)
        return Response(ZatcaSettingsSerializer(settings).data)

    def patch(self, request):
        settings = ZatcaSettings.for_branch(request.user.branch)
        serializer = ZatcaSettingsSerializer(settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ZatcaDevicesView(APIView):
    """
    The branch's ZATCA devices for the Credentials page: the active one first,
    then archived ones. Read-only for owners and managers, and secret-free by
    construction (see ZatcaDeviceSerializer). `encryption` says whether
    ZATCA_ENCRYPTION_KEY is usable, without revealing anything about it.
    """

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        settings = ZatcaSettings.for_branch(request.user.branch)
        devices = ZatcaDevice.objects.filter(branch=request.user.branch)
        configured, problem = crypto.encryption_status()
        return Response({
            "environment": settings.environment,
            "encryption": {"configured": configured, "problem": problem},
            "devices": ZatcaDeviceSerializer(devices, many=True, context={"settings": settings}).data,
        })


# -- receipts (every print point) -------------------------------------------


class PosStatusView(APIView):
    """What the till needs to know, for any cashier: whether invoices are
    being issued, whether they are test ones, and whether company (standard)
    invoices are offered. Nothing else about the settings."""

    permission_classes = [IsCashierStaff]

    def get(self, request):
        settings = ZatcaSettings.objects.filter(branch=request.user.branch).first()
        enabled = bool(settings and settings.enabled and settings.active_device())
        return Response({
            "enabled": enabled,
            "is_test": profile(settings.environment).is_test if settings else True,
            "b2b": enabled and settings.invoice_types in (InvoiceTypes.STANDARD, InvoiceTypes.BOTH),
        })


class ReceiptsView(APIView):
    """GET ?order=<id> or ?bill=<id>: the tax invoices and credit notes that
    cover them, ready to print. Empty while ZATCA is off or nothing was
    invoiced (the POS then prints its ordinary receipt, as before).

    ?document=<id> is exactly that one document — the invoices page reprints
    the row it is on, not everything else on its order."""

    permission_classes = [IsCashierStaff]

    def get(self, request):
        from orders.models import Order

        document_id = request.query_params.get("document")
        if document_id:
            if not document_id.isdigit():
                return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
            document = get_object_or_404(ZatcaDocument, pk=document_id, branch=request.user.branch)
            return Response(receipts.for_document(document))
        orders = Order.objects.filter(branch=request.user.branch)
        if request.query_params.get("order"):
            orders = orders.filter(pk=request.query_params["order"])
        elif request.query_params.get("bill"):
            orders = orders.filter(bill_id=request.query_params["bill"])
        else:
            return Response({"error": "error.zatcaOrderOrBillRequired"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(receipts.for_orders(orders.values_list("pk", flat=True)))


class ReceiptPrintedView(APIView):
    """POST before printing: counts the print. Every print after the first is
    a reprint and carries "Copy"."""

    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        with transaction.atomic():
            document = get_object_or_404(ZatcaDocument.objects.select_for_update(), pk=pk, branch=request.user.branch)
            document.print_count += 1
            document.save(update_fields=["print_count"])
        return Response({"print_count": document.print_count, "copy": document.print_count > 1})


class PublicReceiptView(APIView):
    """The customer's own simplified invoice by its UUID (unguessable, v4),
    for the Moyasar success page's download/share link. Never a standard
    invoice: those carry a company's details."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, uuid):
        document = get_object_or_404(ZatcaDocument, uuid=uuid, simplified=True)
        return Response(receipts.payload(document, public=True))


# -- B2B buyers ---------------------------------------------------------------


class BusinessCustomersView(APIView):
    """GET ?search= (name or VAT) to pick a company; POST to add one, or to
    update it when that VAT number is already known."""

    permission_classes = [IsCashierStaff]

    def get(self, request):
        customers = BusinessCustomer.objects.filter(branch=request.user.branch)
        search = request.query_params.get("search", "").strip()
        if search:
            customers = customers.filter(Q(legal_name__icontains=search) | Q(vat_number__startswith=search))
        return Response(BusinessCustomerSerializer(customers[:20], many=True).data)

    def post(self, request):
        serializer = BusinessCustomerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        customer, _created = BusinessCustomer.objects.update_or_create(
            branch=request.user.branch, vat_number=data["vat_number"], defaults=data,
        )
        return Response(BusinessCustomerSerializer(customer).data, status=status.HTTP_201_CREATED)


def _current_buyer(orders):
    link = ZatcaOrderBuyer.objects.filter(order__in=list(orders)).select_related("buyer").first()
    return Response({"business_customer": BusinessCustomerSerializer(link.buyer).data if link else None})


def _set_buyer(request, orders):
    """Point these orders' coming tax invoice at a company (or back to B2C)."""
    raw = request.data.get("business_customer")
    buyer = None
    if raw not in (None, ""):
        buyer = get_object_or_404(BusinessCustomer, pk=raw, branch=request.user.branch)
    orders = list(orders)
    if ZatcaDocumentOrder.objects.filter(order__in=orders, kind=ubl.DocumentKind.INVOICE).exists():
        return Response({"error": "error.zatcaAlreadyInvoiced"}, status=status.HTTP_409_CONFLICT)
    for order in orders:
        if buyer is None:
            ZatcaOrderBuyer.objects.filter(order=order).delete()
        else:
            ZatcaOrderBuyer.objects.update_or_create(order=order, defaults={"buyer": buyer, "set_by": request.user})
    return Response({"business_customer": BusinessCustomerSerializer(buyer).data if buyer else None})


class OrderBuyerView(APIView):
    """GET the company an order will be invoiced to; POST {business_customer:
    id | null} to change it, before the payment that settles it."""

    permission_classes = [IsCashierStaff]

    def get(self, request, pk):
        from orders.models import Order

        return _current_buyer([get_object_or_404(Order, pk=pk, branch=request.user.branch)])

    def post(self, request, pk):
        from orders.models import Order

        order = get_object_or_404(Order, pk=pk, branch=request.user.branch)
        return _set_buyer(request, [order])


class BillBuyerView(APIView):
    """The same for a table bill: applies to the orders it will settle."""

    permission_classes = [IsCashierStaff]

    def _orders(self, request, pk):
        from orders.models import Bill, OrderStatus, PaymentStatus

        bill = get_object_or_404(Bill, pk=pk, branch=request.user.branch)
        return bill.orders.exclude(status=OrderStatus.CANCELLED).exclude(payment_status=PaymentStatus.PAID)

    def get(self, request, pk):
        return _current_buyer(self._orders(request, pk))

    def post(self, request, pk):
        return _set_buyer(request, self._orders(request, pk))


# -- managers: view and retry --------------------------------------------------


class DocumentsView(APIView):
    """GET ?status=pending|reported|cleared|rejected: the branch's documents,
    newest first, with ZATCA's answers, plus counts per status."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        documents = ZatcaDocument.objects.filter(branch=request.user.branch).select_related("original", "buyer")
        counts = dict(documents.order_by().values("status").annotate(n=Count("id")).values_list("status", "n"))
        wanted = request.query_params.get("status")
        if wanted:
            documents = documents.filter(status=wanted)
        return Response({
            "counts": {value: counts.get(value, 0) for value in DocumentStatus.values},
            "documents": ZatcaDocumentSerializer(documents.order_by("-icv", "-id")[:200], many=True).data,
        })


class DocumentRetryView(APIView):
    """POST: send this pending document to ZATCA now (managers and owners)."""

    permission_classes = [IsOwnerOrManager]

    def post(self, request, pk):
        document = get_object_or_404(ZatcaDocument, pk=pk, branch=request.user.branch)
        if document.status == DocumentStatus.PENDING:
            pos.submit_safely(document.pk)
            document.refresh_from_db()
        return Response(ZatcaDocumentSerializer(document).data)


class DocumentXmlView(APIView):
    """GET ?which=signed|cleared: download a document's XML. "signed" is the
    XML this POS signed (every document); "cleared" is the copy ZATCA sent
    back for a cleared B2B invoice, 404 when there is none."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        document = get_object_or_404(ZatcaDocument, pk=pk, branch=request.user.branch)
        which = request.query_params.get("which", "signed")
        if which == "cleared":
            xml, filename = document.cleared_xml, f"{document.number}_cleared.xml"
        elif which == "signed":
            xml, filename = document.signed_xml, f"{document.number}.xml"
        else:
            return Response({"which": "error.xmlKindInvalid"}, status=status.HTTP_400_BAD_REQUEST)
        if not xml:
            raise Http404
        response = HttpResponse(xml, content_type="application/xml; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


# -- go-live wizard (owners act, managers watch) --------------------------------


def _go_live_payload(go_live):
    from .environments import LEGAL_WARNING

    if go_live is None:
        return None
    env = profile(go_live.environment)
    device = go_live.device
    return {
        "id": go_live.pk,
        "environment": go_live.environment,
        "practice": go_live.practice,
        "status": go_live.status,
        "step": golive.step_of(go_live),
        "details": go_live.details,
        "fixed_fields": list(golive.PRACTICE_FIXED) if go_live.practice else [],
        "device": golive.device_summary(device),
        "checks": go_live.checks,
        "requires_real_identity": env.requires_real_identity,
        "needs_legal_confirmation": env.needs_legal_confirmation,
        "legal_warning": LEGAL_WARNING if env.needs_legal_confirmation else "",
        "legal_confirmed_at": go_live.legal_confirmed_at,
        "default_otp": env.default_otp,
        "activated": go_live.activated,
        "created_at": go_live.created_at,
        "finished_at": go_live.finished_at,
        # The test sales made since: what the last step shows.
        "documents": ZatcaDocumentSerializer(device.documents.order_by("-icv")[:10], many=True).data
        if device is not None and device.is_active else [],
    }


def _go_live_error(exc):
    body = {"error": exc.code}
    if exc.fields:
        body["fields"] = exc.fields
    if exc.detail:
        body["detail"] = exc.detail
    if exc.checks is not None:
        body["checks"] = exc.checks
    return Response(body, status=exc.status)


class GoLiveView(APIView):
    """
    GET: the open wizard (or none), the last finished one, and what the
    branch is on now. POST {environment, confirm_back_to_sandbox}: start.
    PATCH {details...}: save the business details. DELETE: cancel.
    """

    def get_permissions(self):
        return [IsOwnerOrManager()] if self.request.method == "GET" else [IsOwner()]

    def get(self, request):
        branch = request.user.branch
        settings = ZatcaSettings.for_branch(branch)
        last = (ZatcaGoLive.objects.filter(branch=branch).exclude(status=GoLiveStatus.OPEN)
                .select_related("device").first())
        active = settings.active_device()
        return Response({
            "environment": settings.environment,
            "is_test": profile(settings.environment).is_test,
            "active_device": golive.device_summary(active),
            "back_to_sandbox_block": golive.back_to_sandbox_block(settings)
            if settings.environment != "sandbox" else None,
            "current": _go_live_payload(golive.current(branch)),
            "last": _go_live_payload(last),
        })

    def post(self, request):
        try:
            go_live = golive.start(request.user.branch, str(request.data.get("environment", "")), actor=request.user,
                                   confirm_back_to_sandbox=bool(request.data.get("confirm_back_to_sandbox")))
        except golive.GoLiveError as exc:
            return _go_live_error(exc)
        return Response(_go_live_payload(go_live), status=status.HTTP_201_CREATED)

    def patch(self, request):
        return _go_live_step(request, lambda go_live: golive.update_details(go_live, request.data))

    def delete(self, request):
        return _go_live_step(request, lambda go_live: golive.cancel(go_live, actor=request.user))


class DeviceRecheckView(APIView):
    """POST: repeat a live device's compliance checks with its compliance
    CSID (no OTP). Owners only; nothing about the device changes."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        device = get_object_or_404(ZatcaDevice, pk=pk, branch=request.user.branch)
        try:
            checks = golive.recheck(device, actor=request.user)
        except golive.GoLiveError as exc:
            return _go_live_error(exc)
        return Response({"device": golive.device_summary(device), "checks": checks,
                         "passed": all(check["passed"] for check in checks)})


def _go_live_step(request, step):
    go_live = golive.current(request.user.branch)
    if go_live is None:
        return Response({"error": "error.zatcaGoLiveNotOpen"}, status=status.HTTP_404_NOT_FOUND)
    try:
        go_live = step(go_live)
    except golive.GoLiveError as exc:
        return _go_live_error(exc)
    return Response(_go_live_payload(go_live))


class GoLiveStepView(APIView):
    """POST go-live/<step>/: keys, compliance-csid {otp, confirm_legal},
    checks, production-csid, finish {activate}. Owners only."""

    permission_classes = [IsOwner]

    def post(self, request, step):
        actor, data = request.user, request.data
        steps = {
            "keys": lambda go_live: golive.generate_keys(go_live, actor=actor),
            "compliance-csid": lambda go_live: golive.compliance_csid(
                go_live, otp=str(data.get("otp", "")), confirm_legal=bool(data.get("confirm_legal")), actor=actor),
            "checks": lambda go_live: golive.run_checks(go_live, actor=actor),
            "production-csid": lambda go_live: golive.production_csid(go_live, actor=actor),
            "finish": lambda go_live: golive.finish(go_live, activate=bool(data.get("activate")), actor=actor),
        }
        if step not in steps:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
        return _go_live_step(request, steps[step])


class DebitNoteView(APIView):
    """POST {description, amount, reason}: a debit note (383) against this
    invoice, for an undercharge. Owners only; `amount` is what the customer
    still owes, VAT inclusive. Sent to ZATCA like any other document."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        from decimal import Decimal, InvalidOperation

        from .documents import IssueError

        invoice = get_object_or_404(ZatcaDocument, pk=pk, branch=request.user.branch)
        if invoice.kind != ubl.DocumentKind.INVOICE or invoice.status == DocumentStatus.REJECTED:
            return Response({"error": "error.zatcaDebitNeedsInvoice"}, status=status.HTTP_409_CONFLICT)
        description = str(request.data.get("description", "")).strip()[:255]
        reason = str(request.data.get("reason", "")).strip()[:255]
        errors = {}
        try:
            amount = Decimal(str(request.data.get("amount", ""))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            amount = None
        if amount is None or not (Decimal("0.01") <= amount <= Decimal("1000000")):
            errors["amount"] = "error.zatcaDebitAmountInvalid"
        if not description:
            errors["description"] = "error.zatcaFieldRequired"
        if not reason:
            errors["reason"] = "error.zatcaFieldRequired"
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            note = pos.debit_note(invoice, description=description, amount=amount, reason=reason)
        except IssueError:
            return Response({"error": "error.zatcaNoActiveDevice"}, status=status.HTTP_409_CONFLICT)
        pos.dispatch(note)
        note.refresh_from_db()
        return Response(ZatcaDocumentSerializer(note).data, status=status.HTTP_201_CREATED)
