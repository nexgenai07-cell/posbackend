from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from branches.models import Branch
from branches.serializers import BranchSerializer
from common.mixins import BranchScopedQuerysetMixin
from common.geofence import validate_branch_location

from .models import Shift, Staff, StaffRole
from .permissions import IsOwnerOrManager
from .serializers import (
    LoginStaffChoiceSerializer,
    PinLoginSerializer,
    ShiftSerializer,
    StaffSerializer,
)


class PinLoginView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "pin-login"

    def post(self, request):
        serializer = PinLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        staff = serializer.validated_data["staff"]

        refresh = RefreshToken.for_user(staff)
        refresh["role"] = staff.role
        refresh["branch_id"] = staff.branch_id

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "staff": StaffSerializer(staff).data,
        })

class LoginChoicesView(APIView):
    """
    Everything the login screen needs *before* it holds a token: which branch
    this terminal is for, and who to put in the name picker.

    This exists because the two obvious endpoints both require auth:
    `branches/active/` is `IsAuthenticated` and `staff/` is owner/manager-only,
    while the login screen by definition has no token yet. Loosening either of
    those to serve the login screen would have opened the whole staff/branch
    resource — a dedicated public endpoint exposes exactly a name, a role and
    a branch name instead.

    `?branch=<id>` picks the branch; with no query param it falls back to the
    lowest-id branch, which is the single seeded one until multi-branch
    terminal selection is a real flow. Only `is_active` staff are returned, so
    a deactivated account disappears from the picker (and pin-login rejects it
    anyway — see PinLoginSerializer).

    Known trade-off, accepted deliberately: staff names are readable by anyone
    who can reach this endpoint. That is inherent to a name-picker login, not
    a leak of credentials — no PIN material is returned by anything here, and
    pin-login itself is rate-limited (see SIMPLE_JWT/throttle settings).
    """

    permission_classes = [AllowAny]
    throttle_scope = "login-choices"

    def get(self, request):
        branch_id = request.query_params.get("branch")
        if branch_id:
            # 404s for an unknown branch id rather than silently serving the
            # default one, so a misconfigured terminal is loud instead of
            # quietly logging staff into the wrong branch.
            branch = get_object_or_404(Branch.objects.all(), pk=branch_id)
        else:
            branch = Branch.objects.order_by("id").first()
            if branch is None:
                return Response({"error": "error.noBranch"}, status=status.HTTP_404_NOT_FOUND)

        staff = branch.staff.filter(is_active=True).order_by("name")
        return Response({
            "branch": BranchSerializer(branch).data,
            "staff": LoginStaffChoiceSerializer(staff, many=True).data,
        })




class RefreshView(APIView):
    """
    Not SimpleJWT's built-in TokenRefreshView: that serializer re-validates
    the token's user-id claim against get_user_model() (auth.User), which
    Staff isn't. This does the same job — mint a new access token from a
    valid refresh token — without that lookup.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        try:
            refresh = RefreshToken(request.data["refresh"])
        except (KeyError, TokenError):
            return Response({"error": "error.invalidToken"}, status=status.HTTP_401_UNAUTHORIZED)
        return Response({"access": str(refresh.access_token)})


class LogoutView(APIView):
    """
    No server-side token blacklist (see config/settings.py) — logout is the
    client discarding its tokens. This endpoint exists for API symmetry and
    so the frontend has a clear call to make, but it's a no-op here.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response(status=status.HTTP_205_RESET_CONTENT)


def _is_self_or_manager(request, staff):
    return request.user.pk == staff.pk or request.user.role in (StaffRole.OWNER, StaffRole.MANAGER)


class StaffViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """CRUD is owner/manager only. clock-in/clock-out relax that to 'self or
    a manager', overriding permission_classes per-action below. Queryset is
    scoped to the requester's own branch — an owner can't see or edit
    another branch's staff."""

    queryset = Staff.objects.select_related("branch").all()
    serializer_class = StaffSerializer
    permission_classes = [IsOwnerOrManager]

    @action(detail=True, methods=["get"], url_path="shifts", permission_classes=[IsAuthenticated])
    def shifts(self, request, pk=None):
        staff = self.get_object()
        if not _is_self_or_manager(request, staff):
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)
        shifts = staff.shifts.order_by("-clock_in")
        return Response(ShiftSerializer(shifts, many=True).data)

    @action(detail=True, methods=["post"], url_path="clock-in", permission_classes=[IsAuthenticated])
    def clock_in(self, request, pk=None):
        staff = self.get_object()
        if not _is_self_or_manager(request, staff):
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)
        validate_branch_location(staff.branch, request.data, radius_m=staff.branch.attendance_radius_m)
        if staff.shifts.filter(clock_out__isnull=True).exists():
            return Response({"error": "error.alreadyClockedIn"}, status=status.HTTP_400_BAD_REQUEST)
        shift = Shift.objects.create(staff=staff, clock_in=timezone.now())
        return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="clock-out", permission_classes=[IsAuthenticated])
    def clock_out(self, request, pk=None):
        staff = self.get_object()
        if not _is_self_or_manager(request, staff):
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)
        validate_branch_location(staff.branch, request.data, radius_m=staff.branch.attendance_radius_m)
        shift = staff.shifts.filter(clock_out__isnull=True).order_by("-clock_in").first()
        if not shift:
            return Response({"error": "error.notClockedIn"}, status=status.HTTP_400_BAD_REQUEST)
        shift.clock_out = timezone.now()
        shift.save(update_fields=["clock_out", "updated_at"])
        return Response(ShiftSerializer(shift).data)
