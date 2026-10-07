from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.shortcuts import get_object_or_404
from django.db import transaction
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

from .models import Shift, ShiftTemplate, Staff, StaffRole
from .permissions import IsOwnerOrManager
from .serializers import (
    LoginStaffChoiceSerializer,
    PinLoginSerializer,
    AttendanceSettingsSerializer,
    ShiftSerializer,
    ShiftTemplateSerializer,
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


def _branch_timezone(branch):
    try:
        return ZoneInfo(branch.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.get_default_timezone()


def _attendance_day_bounds(branch, now):
    zone = _branch_timezone(branch)
    local_now = timezone.localtime(now, zone)
    start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    end = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=zone)
    return local_now, start, end


def _is_late_for_staff(staff, local_now):
    template = staff.shift_template
    if template is None or not template.active:
        return False
    scheduled_date = local_now.date()
    if template.crosses_midnight and local_now.time() < template.end_time:
        scheduled_date -= timedelta(days=1)
    elif local_now.time() < template.start_time:
        return False
    start = datetime.combine(scheduled_date, template.start_time, tzinfo=local_now.tzinfo)
    threshold = start + timedelta(minutes=staff.branch.late_threshold_minutes)
    return local_now > threshold


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
        with transaction.atomic():
            # Lock only Staff: shift_template is nullable, and PostgreSQL
            # rejects FOR UPDATE on the nullable side of select_related's join.
            staff = Staff.objects.select_for_update(of=("self",)).select_related("branch", "shift_template").get(pk=staff.pk)
            if staff.shifts.filter(clock_out__isnull=True).exists():
                return Response({"error": "error.alreadyClockedIn"}, status=status.HTTP_400_BAD_REQUEST)
            now = timezone.now()
            local_now, day_start, day_end = _attendance_day_bounds(staff.branch, now)
            today_count = staff.shifts.filter(clock_in__gte=day_start, clock_in__lt=day_end).count()
            if today_count >= staff.branch.max_clock_ins_per_day:
                return Response({"error": "error.maxDailyClockInsReached"}, status=status.HTTP_400_BAD_REQUEST)
            shift = Shift.objects.create(staff=staff, clock_in=now, is_late=_is_late_for_staff(staff, local_now))
        return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="clock-out", permission_classes=[IsAuthenticated])
    def clock_out(self, request, pk=None):
        staff = self.get_object()
        if not _is_self_or_manager(request, staff):
            return Response({"error": "error.forbidden"}, status=status.HTTP_403_FORBIDDEN)
        validate_branch_location(staff.branch, request.data, radius_m=staff.branch.attendance_radius_m)
        with transaction.atomic():
            staff = Staff.objects.select_for_update().select_related("branch").get(pk=staff.pk)
            shift = staff.shifts.filter(clock_out__isnull=True).order_by("-clock_in").first()
            if not shift:
                return Response({"error": "error.notClockedIn"}, status=status.HTTP_400_BAD_REQUEST)
            now = timezone.now()
            _local_now, day_start, day_end = _attendance_day_bounds(staff.branch, now)
            today_count = staff.shifts.filter(clock_out__gte=day_start, clock_out__lt=day_end).count()
            if today_count >= staff.branch.max_clock_outs_per_day:
                return Response({"error": "error.maxDailyClockOutsReached"}, status=status.HTTP_400_BAD_REQUEST)
            shift.clock_out = now
            shift.save(update_fields=["clock_out", "updated_at"])
        return Response(ShiftSerializer(shift).data)


class ShiftTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = ShiftTemplateSerializer
    permission_classes = [IsOwnerOrManager]

    def get_queryset(self):
        return ShiftTemplate.objects.filter(branch_id=self.request.user.branch_id).prefetch_related("assigned_staff")


class AttendanceSettingsView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        return Response(AttendanceSettingsSerializer(request.user.branch).data)

    def patch(self, request):
        serializer = AttendanceSettingsSerializer(request.user.branch, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
