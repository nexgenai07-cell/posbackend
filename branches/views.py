from rest_framework.generics import RetrieveAPIView, UpdateAPIView
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import IsOwnerOrManager

from .models import Branch
from .serializers import BranchLocationSerializer, BranchSerializer


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
