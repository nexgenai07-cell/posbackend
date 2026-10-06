from rest_framework.generics import RetrieveAPIView, UpdateAPIView
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated

from accounts.permissions import IsOwnerOrManager

from .models import Branch
from .serializers import BranchLocationSerializer, BranchSerializer, PublicBranchSerializer


class ActiveBranchView(RetrieveAPIView):
    """'Active branch' means the requesting staff's own branch — not just
    "the only row", which would leak another branch's info once a second
    branch exists."""

    serializer_class = BranchSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user.branch


class BranchUpdateView(UpdateAPIView):
    """Scoped to the requester's own branch — PATCHing another branch's id
    404s rather than succeeding or leaking that it exists."""

    serializer_class = BranchSerializer
    permission_classes = [IsOwnerOrManager]
    http_method_names = ["patch"]

    def get_queryset(self):
        return Branch.objects.filter(pk=self.request.user.branch_id)


class BranchLocationView(APIView):
    """Location coordinates are exposed only through this manager-only API."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        return Response(BranchLocationSerializer(request.user.branch).data)

    def patch(self, request):
        serializer = BranchLocationSerializer(request.user.branch, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PublicBranchView(APIView):
    """
    Public (AllowAny) — the restaurant's own name and logo, so a customer-facing
    client can brand itself from configuration instead of hard-coding it.

    `?branch=` is optional and falls back to the lowest id, the same convention
    PublicMenuView and LoginChoicesView already use for the single-branch case.
    Throttled under the existing "menu" scope: it is read-only public data
    fetched about as often as the menu is.
    """

    permission_classes = [AllowAny]
    throttle_scope = "menu"

    def get(self, request):
        branch_id = request.query_params.get("branch")
        if branch_id:
            branch = Branch.objects.filter(pk=branch_id).first()
        else:
            branch = Branch.objects.order_by("id").first()
        if branch is None:
            return Response({"error": "error.noBranch"}, status=404)
        return Response(PublicBranchSerializer(branch).data)
