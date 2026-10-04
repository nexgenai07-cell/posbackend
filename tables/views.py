from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import transaction

from accounts.permissions import IsOwnerOrManager, IsPOSStaff, IsTableReleaseStaff
from common.mixins import BranchScopedQuerysetMixin

# Imported here (views), not in tables/models.py, to keep the model layer's
# dependency one-directional (orders depends on tables, not the reverse).
from orders.models import Order, OrderStatus
from orders.serializers import PublicOrderSerializer

from .models import Table, TableStatus
from .serializers import TableSerializer
from .services import parse_qr_payload


class TableViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    CRUD (adding/renaming/removing physical tables) is owner/manager only —
    the admin Tables page. open/close/needs-bill are POS actions, so any
    POS-area staff (owner/manager/cashier) can use them, matching
    restaurant-admin's rbac.ts AREA_ROLES for "pos". Read is any
    authenticated staff (Table Map is used by more than just managers).
    Queryset scoped to the requester's branch.
    """

    queryset = Table.objects.all()
    serializer_class = TableSerializer

    def get_permissions(self):
        # active_sessions is a read: it exposes exactly what list/retrieve
        # already return (TableSerializer includes session_token), just
        # filtered to open tables — so it matches read's IsAuthenticated
        # rather than the owner/manager write gate.
        if self.action in ("list", "retrieve", "active_sessions"):
            return [IsAuthenticated()]
        if self.action == "close_table":
            return [IsTableReleaseStaff()]
        if self.action in ("open_table", "needs_bill"):
            return [IsPOSStaff()]
        return [IsOwnerOrManager()]

    def destroy(self, request, *args, **kwargs):
        table = self.get_object()
        has_open_order = Order.objects.filter(table=table).exclude(
            status__in=[OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED]
        ).exists()
        if table.status != TableStatus.EMPTY or has_open_order:
            return Response({"error": "error.tableNotEmpty"}, status=status.HTTP_409_CONFLICT)
        table.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="open")
    def open_table(self, request, pk=None):
        table = self.get_object()
        table.open()
        return Response(TableSerializer(table).data)

    @action(detail=True, methods=["post"], url_path="close")
    def close_table(self, request, pk=None):
        with transaction.atomic():
            table = get_object_or_404(
                Table.objects.select_for_update().filter(branch=request.user.branch), pk=pk
            )
            has_open_order = Order.objects.filter(table=table).exclude(
                status__in=[OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED]
            ).exists()
            if has_open_order:
                return Response({"error": "error.tableHasOpenOrder"}, status=status.HTTP_409_CONFLICT)
            table.close()
        return Response(TableSerializer(table).data)

    @action(detail=True, methods=["post"], url_path="needs-bill")
    def needs_bill(self, request, pk=None):
        table = self.get_object()
        table.mark_needs_bill()
        return Response(TableSerializer(table).data)

    @action(detail=False, methods=["get"], url_path="active-sessions")
    def active_sessions(self, request):
        """
        Every currently occupied table, with its permanent QR payload and token.

        This exists so staff can retrieve the code a customer's phone needs
        without opening Django admin or a shell: to reprint a QR label whose
        original is lost, or to read out the code for restaurant-mobile's
        manual-entry fallback when a scan won't take. Only open tables appear:
        Table.close() nulls session_token, so a closed table's old code is
        gone from here too, which is the point (it's invalid anyway).
        """
        tables = list(self.get_queryset().exclude(status=TableStatus.EMPTY).order_by("id"))
        return Response({"count": len(tables), "results": TableSerializer(tables, many=True).data})


class TableBySessionView(APIView):
    """
    Public (AllowAny) — resolves a scanned QR code's session token to its
    table and current order, for restaurant-mobile's customer-facing flow
    (Phase 13). A literal token string never matches the session_token
    column's NULL rows, so a closed/never-opened table can't be found this
    way without any extra exclusion. Doubles as the "refresh my order
    status" call: the mobile app re-calls this after every real-time event
    from /ws/table/ instead of needing a second endpoint.

    The token arrives here as a *path segment*, so it is always the bare form
    — a "/" can't survive in one (Django's `str` converter stops at it). The
    permissive "/t/<token>"-or-bare handling lives on the POST side instead
    (PublicOrderCreateView's `session_token` body field), where the raw scan
    can genuinely be passed through; see tables/services.py's
    parse_qr_payload(). Here it is still applied as a cheap normaliser —
    trailing whitespace from a pasted code, or outright garbage, is treated
    as "not found" rather than querying with it.
    """

    permission_classes = [AllowAny]
    throttle_scope = "table-lookup"

    def get(self, request, token):
        token = parse_qr_payload(token)
        # Blank/garbage input is just "no such table": reusing Http404 here
        # keeps the response body ({error.notFound} via common/exceptions.py)
        # identical to an unknown token, so clients have one shape to handle.
        if not token:
            raise Http404
        table = get_object_or_404(Table, session_token=token)
        order = (
            Order.objects.filter(table=table)
            .exclude(status__in=[OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED])
            .prefetch_related("items")
            .order_by("-opened_at", "-id")
            .first()
        )
        return Response({
            "table": TableSerializer(table).data,
            "open_order": PublicOrderSerializer(order).data if order else None,
        })
