from django.db.models import F
from rest_framework import status, viewsets
from rest_framework.response import Response

from accounts.permissions import IsOwnerOrManager, IsPOSStaff
from common.mixins import BranchScopedQuerysetMixin

from .models import Customer
from .serializers import CustomerSerializer
from .services import find_or_create_customer


class CustomerViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Browsing/editing customers is owner/manager; the POS payment screen's
    find-or-create is any POS-area staff. Scoped to the requester's branch,
    like every other branch-scoped viewset.

    Phase 13 still owns the *public* QR flow that will create most customers —
    this exists so the "add a phone number to this bill" step the admin's
    Payment screen has always had keeps working against the real API.
    """

    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsPOSStaff()]
        return [IsOwnerOrManager()]

    def get_queryset(self):
        qs = super().get_queryset()
        phone = self.request.query_params.get("phone")
        if phone:
            qs = qs.filter(phone=phone)
        # nulls_last: Postgres puts NULLs first on a DESC sort otherwise, which
        # would float every never-ordered customer to the top of the list.
        return qs.order_by(F("last_order_at").desc(nulls_last=True), "-id")

    def create(self, request, *args, **kwargs):
        """
        Find-or-create by phone: 200 with the existing row, 201 when new.

        Same convention as POST /api/orders/ (also a get-or-create), so a POS
        client can call this on every payment without first asking whether the
        guest is already known. See customers/services.py's
        find_or_create_customer() — shared with the public QR order endpoint.
        """
        phone = str(request.data.get("phone", "")).strip()
        if not phone:
            return Response({"error": "error.phoneRequired"}, status=status.HTTP_400_BAD_REQUEST)

        customer, created = find_or_create_customer(request.user.branch, phone, request.data.get("name", ""))
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(self.get_serializer(customer).data, status=status_code)

    def perform_update(self, serializer):
        # A customer belongs to the branch that created them — branch isn't a
        # movable field (the same "scope it from the start" lesson as the
        # Phase 5 branch-scoping retrofit).
        serializer.save(branch=self.request.user.branch)

