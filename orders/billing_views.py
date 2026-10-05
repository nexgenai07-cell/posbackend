"""
Billing endpoints: the customer's pay flow, Moyasar verification, and the
cashier's settle/release actions.

Kept out of orders/views.py, which is already long — same split as
billing.py/workflow.py. Routed from orders/urls.py.

Public endpoints here are AllowAny and authenticated by the table's
session_token, exactly as TableBySessionView and PublicOrderCreateView
already are: the token is a CSPRNG secret printed on one table's QR, so
holding it is the proof that you are sitting at that table.
"""

import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsCashierStaff
from tables.models import Table
from tables.services import parse_qr_payload

from . import billing, moyasar
from .billing_serializers import (
    BillSerializer,
    CancelPaySerializer,
    MoyasarPaymentSerializer,
    RequestPaySerializer,
    SettleBillSerializer,
    VerifyOnlineSerializer,
)
from .models import Bill, BillStatus

logger = logging.getLogger("orders.billing")


def _bill_queryset():
    return Bill.objects.select_related("table", "branch").prefetch_related(
        "orders__items", "moyasar_payments"
    )


def _table_for_token(raw_token):
    """Resolve the scanned QR payload to a table, or None."""
    token = parse_qr_payload(raw_token)
    if not token:
        return None
    return Table.objects.filter(session_token=token).select_related("branch").first()


def _transition_error(exc):
    return Response({"error": exc.code}, status=status.HTTP_409_CONFLICT)


def _bill_response(bill, http_status=status.HTTP_200_OK, **extra):
    data = BillSerializer(_bill_queryset().get(pk=bill.pk)).data
    data.update(extra)
    return Response(data, status=http_status)


# -- customer-facing (AllowAny, table session token) -------------------------


class BillBySessionView(APIView):
    """
    GET /api/bills/by-session/<token>/

    The open bill for the scanned table, with a server-computed total. This is
    what the customer's phone polls on the pay screen.
    """

    permission_classes = [AllowAny]
    throttle_scope = "table-lookup"

    def get(self, request, token):
        table = _table_for_token(token)
        if table is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
        bill = billing.current_bill_for_table(table)
        if bill is None:
            return Response({"error": "error.billNotFound"}, status=status.HTTP_404_NOT_FOUND)
        return _bill_response(bill)


class BillRequestPayView(APIView):
    """
    POST /api/bills/request-pay/   {session_token, method: "cash"|"online"}

    open -> pay_requested, which locks the bill against new orders.

    For method=online the response also carries the PUBLISHABLE key, the
    callback_url and the amount in halalas, so the customer web app needs no
    Moyasar configuration of its own and going live is a backend .env change.
    The secret key is never part of any response.
    """

    permission_classes = [AllowAny]
    throttle_scope = "bill-pay"

    def post(self, request):
        serializer = RequestPaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = serializer.validated_data["method"]

        if method == billing.BillMethod.ONLINE and not moyasar.is_configured():
            return Response(
                {"error": "error.paymentsNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        table = _table_for_token(serializer.validated_data["session_token"])
        if table is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            # Lock the table first, then the bill — the same order
            # PublicOrderCreateView takes, so the two can't deadlock.
            table = Table.objects.select_for_update().get(pk=table.pk)
            bill = Bill.objects.select_for_update().filter(table=table).exclude(
                status=BillStatus.CLOSED
            ).first()
            if bill is None:
                return Response({"error": "error.billNotFound"}, status=status.HTTP_404_NOT_FOUND)
            try:
                billing.request_payment(bill, method)
            except billing.BillTransitionError as exc:
                return _transition_error(exc)

        extra = {}
        if method == billing.BillMethod.ONLINE:
            extra = {
                "publishable_key": moyasar.publishable_key(),
                "callback_url": _callback_url(table),
                "description": f"{bill.bill_code} — {table.label_en}",
            }
        return _bill_response(bill, **extra)


def _callback_url(table):
    """
    Where Moyasar sends the customer back to. The table's session token rides
    along as `t=` so the callback page knows which table to return to —
    Moyasar appends its own id/status/message params and leaves existing ones
    intact. The token is not what authorises the settle (the verified
    metadata.bill_id is), it is only navigation.
    """
    from django.conf import settings
    base = settings.CUSTOMER_WEB_BASE_URL.rstrip("/")
    return f"{base}/pay/callback?t={table.session_token}"


class BillCancelPayView(APIView):
    """
    POST /api/bills/cancel-pay-request/   {session_token}

    pay_requested -> open, from the customer's side: they tapped Pay by
    mistake, or want to add dessert after all. Refused once paid.
    """

    permission_classes = [AllowAny]
    throttle_scope = "bill-pay"

    def post(self, request):
        serializer = CancelPaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        table = _table_for_token(serializer.validated_data["session_token"])
        if table is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            bill = Bill.objects.select_for_update().filter(table=table).exclude(
                status=BillStatus.CLOSED
            ).first()
            if bill is None:
                return Response({"error": "error.billNotFound"}, status=status.HTTP_404_NOT_FOUND)
            try:
                billing.reopen_bill(bill)
            except billing.BillTransitionError as exc:
                return _transition_error(exc)
        return _bill_response(bill)


class BillVerifyOnlineView(APIView):
    """
    POST /api/bills/verify-online/   {session_token, payment_id}

    Called by the callback page through burger_web's BFF. The ?status= on the
    redirect is never read — only the id is, and only to look the payment up.

    A bill is marked paid ONLY IF all four hold:
      1. Moyasar's own API reports status == "paid"
      2. amount == the bill total in halalas, recomputed under a row lock
      3. metadata.bill_id == that bill's id
      4. the bill belongs to the table the session_token resolves to
    """

    permission_classes = [AllowAny]
    throttle_scope = "bill-verify"

    def post(self, request):
        serializer = VerifyOnlineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment_id = serializer.validated_data["payment_id"]

        table = _table_for_token(serializer.validated_data["session_token"])
        if table is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)

        try:
            payload = moyasar.fetch_payment(payment_id)
        except moyasar.MoyasarPaymentNotFound:
            return Response({"error": "error.paymentVerificationFailed"}, status=status.HTTP_404_NOT_FOUND)
        except moyasar.MoyasarNotConfigured:
            return Response({"error": "error.paymentsNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except moyasar.MoyasarUnreachable:
            return Response({"error": "error.paymentGatewayUnreachable"}, status=status.HTTP_502_BAD_GATEWAY)

        outcome, bill, error = settle_from_moyasar(payload, expected_table=table)
        if error:
            return error
        return _bill_response(bill, result=outcome)


class MoyasarWebhookView(APIView):
    """
    POST /api/payments/moyasar/webhook/

    Moyasar's server-to-server notification. This is the path that makes
    payment reliable when the customer closes the browser on the 3DS page.

    The body is treated as nothing but a hint that a payment id exists: the
    payment is re-fetched from the Moyasar API and re-verified exactly as the
    callback path is, so a forged webhook cannot settle anything.

    Status codes are chosen for Moyasar's retry behaviour: 2xx stops retries,
    5xx asks for another attempt.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # no staff JWT; this is a machine caller

    def post(self, request):
        payload = request.data if isinstance(request.data, dict) else {}

        if not moyasar.webhook_secret_matches(payload):
            logger.warning("Rejected Moyasar webhook with a bad secret_token")
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)

        data = payload.get("data") or {}
        payment_id = data.get("id") or payload.get("id")
        if not payment_id:
            logger.warning("Moyasar webhook carried no payment id: type=%s", payload.get("type"))
            return Response({"error": "error.paymentIdRequired"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            verified = moyasar.fetch_payment(str(payment_id))
        except moyasar.MoyasarPaymentNotFound:
            # Nothing to retry: Moyasar itself does not know this id.
            logger.warning("Moyasar webhook referenced unknown payment %s", payment_id)
            return Response({"result": "unknown_payment"}, status=status.HTTP_200_OK)
        except (moyasar.MoyasarNotConfigured, moyasar.MoyasarUnreachable):
            # Our side is broken, not theirs — 500 so Moyasar retries later.
            logger.exception("Could not verify webhook payment %s", payment_id)
            return Response({"error": "error.paymentGatewayUnreachable"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        outcome, _bill, error = settle_from_moyasar(verified)
        if error:
            # Malformed/unmatched: 200 so Moyasar stops retrying something
            # that will never succeed. It is already logged, and a paid-but-
            # unmatched payment has been flagged for review by now.
            return Response({"result": "ignored"}, status=status.HTTP_200_OK)
        return Response({"result": outcome}, status=status.HTTP_200_OK)


def settle_from_moyasar(payload, expected_table=None):
    """
    Shared by the callback and the webhook: turn a VERIFIED Moyasar payment
    payload into a settled bill, or into a refusal.

    Returns (outcome, bill, error_response). Exactly one of outcome/error is
    meaningful.
    """
    metadata = payload.get("metadata") or {}
    raw_bill_id = metadata.get("bill_id")
    try:
        bill_id = int(raw_bill_id)
    except (TypeError, ValueError):
        logger.error("Moyasar payment %s has no usable metadata.bill_id (%r)",
                     payload.get("id"), raw_bill_id)
        return None, None, Response({"error": "error.paymentVerificationFailed"},
                                    status=status.HTTP_400_BAD_REQUEST)

    bill = _bill_queryset().filter(pk=bill_id).first()
    if bill is None:
        logger.error("Moyasar payment %s names unknown bill %s", payload.get("id"), bill_id)
        return None, None, Response({"error": "error.billNotFound"}, status=status.HTTP_404_NOT_FOUND)

    # The bill named in the payment metadata must be the bill of the table
    # whose QR token the caller presented. Without this, someone holding their
    # own table's token could post another table's payment id.
    if expected_table is not None and bill.table_id != expected_table.pk:
        logger.error("Payment %s is for bill %s (table %s), not table %s",
                     payload.get("id"), bill.pk, bill.table_id, expected_table.pk)
        return None, None, Response({"error": "error.paymentVerificationFailed"},
                                    status=status.HTTP_400_BAD_REQUEST)

    if payload.get("status") != "paid":
        # Declined / insufficient funds / still authorizing. The bill stays
        # pay_requested so the customer can simply try again.
        logger.info("Moyasar payment %s for bill %s is %s, not paid",
                    payload.get("id"), bill.pk, payload.get("status"))
        return "failed", bill, None

    outcome, bill = billing.mark_bill_paid(
        bill.pk, billing.BillMethod.ONLINE, moyasar=payload
    )
    return outcome, bill, None


# -- cashier-facing (JWT, owner/manager/cashier) -----------------------------


class BillListView(ListAPIView):
    """
    GET /api/bills/?status=pay_requested

    The cashier's settlement queue. Exists because WebSocket notifications are
    lossy across a page reload — this is what the screen renders on mount.
    Branch-scoped: a cashier never sees another branch's bills.
    """

    serializer_class = BillSerializer
    permission_classes = [IsCashierStaff]

    def get_queryset(self):
        queryset = _bill_queryset().filter(branch=self.request.user.branch)
        requested = self.request.query_params.getlist("status")
        if requested:
            queryset = queryset.filter(status__in=requested)
        else:
            # Default view: everything still needing a cashier's attention.
            queryset = queryset.exclude(status=BillStatus.CLOSED)
        return queryset.order_by("-pay_requested_at", "-id")


class BillDetailView(APIView):
    permission_classes = [IsCashierStaff]

    def get(self, request, pk):
        bill = get_object_or_404(_bill_queryset(), pk=pk, branch=request.user.branch)
        data = BillSerializer(bill).data
        data["moyasar_payments"] = MoyasarPaymentSerializer(bill.moyasar_payments.all(), many=True).data
        return Response(data)


class BillMarkPaidView(APIView):
    """
    POST /api/bills/<id>/mark-paid/   {method: "cash"|"card"}

    The cashier took the money at the till — notes in the drawer, or a card on
    the restaurant's own terminal. Online payments never come through here;
    they can only be settled by verifying a real Moyasar payment.
    """

    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        serializer = SettleBillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = serializer.validated_data["method"]

        bill = get_object_or_404(Bill.objects.filter(branch=request.user.branch), pk=pk)
        if bill.status == BillStatus.CLOSED:
            return Response({"error": "error.billClosed"}, status=status.HTTP_409_CONFLICT)
        if bill.status == BillStatus.PAID:
            # Idempotent: a cashier double-tapping "Cash Received" is not an
            # error, and must not write a second set of Payment rows.
            return _bill_response(bill, result=billing.ALREADY_PROCESSED)

        outcome, bill = billing.mark_bill_paid(bill.pk, method, actor=request.user)
        return _bill_response(bill, result=outcome)


class BillReopenView(APIView):
    """
    POST /api/bills/<id>/reopen/

    Cashier-side unlock for a stuck bill: pay_requested -> open, so the table
    can order again. Refused once paid — by then money has moved and the fix
    is a refund, not a state change.
    """

    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        with transaction.atomic():
            bill = get_object_or_404(
                Bill.objects.select_for_update().filter(branch=request.user.branch), pk=pk
            )
            try:
                billing.reopen_bill(bill, actor=request.user)
            except billing.BillTransitionError as exc:
                return _transition_error(exc)
        return _bill_response(bill)


class BillReleaseTableView(APIView):
    """
    POST /api/bills/<id>/release-table/

    paid -> closed, every order closed, and the table goes back to empty and
    free to seat. Requires the bill to be paid.
    """

    permission_classes = [IsCashierStaff]

    def post(self, request, pk):
        with transaction.atomic():
            bill = get_object_or_404(
                # select_related("table") would be an outer join (table is
                # nullable) and Postgres rejects FOR UPDATE on it — see the
                # note in billing.mark_bill_paid().
                Bill.objects.select_for_update().filter(branch=request.user.branch),
                pk=pk,
            )
            try:
                billing.release_table(bill, actor=request.user)
            except billing.BillTransitionError as exc:
                return _transition_error(exc)
        return _bill_response(bill)
