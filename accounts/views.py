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
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import RefreshToken

from branches.models import Branch
from branches.serializers import BranchSerializer
from common.mixins import BranchScopedQuerysetMixin
from common.geofence import validate_branch_location

from .authentication import resolve_staff
from .models import Shift, ShiftTemplate, Staff, StaffRole
from .permissions import IsOwner, IsOwnerOrManager
from .serializers import (
    AttendanceSettingsSerializer,
    ChangePasswordSerializer,
    PasswordLoginSerializer,
    ShiftSerializer,
    ShiftTemplateSerializer,
    StaffSerializer,
    TemporaryPasswordSerializer,
)
from .services import LoginRejected, authenticate_staff, change_own_password, reset_password, unlock_staff


def _issue_tokens(staff):
    refresh = RefreshToken.for_user(staff)
    refresh["role"] = staff.role
    refresh["branch_id"] = staff.branch_id
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class LoginView(APIView):
    """
    POST /api/auth/login/ {username, password}.

    * 200: tokens + the staff record. If `staff.must_change_password` is set
      the access token only opens change-password/logout/me (see
      accounts/authentication.py) until the staff member picks their own.
    * 400 error.invalidCredentials: unknown username, inactive account or
      wrong password - deliberately the same code, so the response doesn't
      confirm which usernames exist. A wrong password on a real account
      also carries `attempts_left` (2, then 1).
    * 423 error.accountLocked: three wrong passwords in a row, or any
      attempt - even the right password - while locked. Only an Owner
      (/staff/<id>/unlock/) or `manage.py unlock_staff` clears it.
    * 429: more than the `login` throttle scope allows from one IP.
    """

    permission_classes = [AllowAny]
    throttle_scope = "login"

    def post(self, request):
        serializer = PasswordLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            staff = authenticate_staff(**serializer.validated_data)
        except LoginRejected as rejected:
            body = {"error": rejected.code}
            if rejected.attempts_left is not None:
                body["attempts_left"] = rejected.attempts_left
            return Response(body, status=rejected.status_code)

        return Response({**_issue_tokens(staff), "staff": StaffSerializer(staff).data})


class LoginChoicesView(APIView):
    """
    What the login screen needs *before* it holds a token: which branch this
    terminal is for (name and logo). Nothing else - in particular no staff
    list: the old name picker handed every visitor half of each credential.

    `branches/active/` is `IsAuthenticated`, and loosening it to serve the
    login screen would open the whole branch resource; this public endpoint
    exposes exactly the branch's display fields instead.

    `?branch=<id>` picks the branch; with no query param it falls back to the
    lowest-id branch, which is the single seeded one until multi-branch
    terminal selection is a real flow.
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

        return Response({"branch": BranchSerializer(branch).data})


class RefreshView(APIView):
    """
    Not SimpleJWT's built-in TokenRefreshView: that serializer re-validates
    the token's user-id claim against get_user_model() (auth.User), which
    Staff isn't. This does the same job - mint a new access token from a
    valid refresh token - and applies the same "still allowed in" rule as
    StaffJWTAuthentication, so a locked or deactivated account can't keep
    minting access tokens from an old refresh token.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        try:
            refresh = RefreshToken(request.data["refresh"])
            staff_id = refresh[jwt_settings.USER_ID_CLAIM]
        except (KeyError, TokenError):
            return Response({"error": "error.invalidToken"}, status=status.HTTP_401_UNAUTHORIZED)
        if resolve_staff(staff_id) is None:
            return Response({"error": "error.invalidToken"}, status=status.HTTP_401_UNAUTHORIZED)
        return Response({"access": str(refresh.access_token)})


class LogoutView(APIView):
    """
    No server-side token blacklist (see config/settings.py) - logout is the
    client discarding its tokens. This endpoint exists for API symmetry and
    so the frontend has a clear call to make, but it's a no-op here.
    """

    permission_classes = [IsAuthenticated]
    allow_password_change_required = True

    def post(self, request):
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """The signed-in staff record, e.g. to re-check `must_change_password`
    after a page reload."""

    permission_classes = [IsAuthenticated]
    allow_password_change_required = True

    def get(self, request):
        return Response(StaffSerializer(request.user).data)


class ChangePasswordView(APIView):
    """
    POST /api/auth/change-password/ {current_password, new_password}.
    The one write a `must_change_password` token is allowed to make; a
    success clears that flag so the rest of the API opens up.
    """

    permission_classes = [IsAuthenticated]
    allow_password_change_required = True

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        staff = change_own_password(request.user, serializer.validated_data["new_password"])
        return Response(StaffSerializer(staff).data)


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
    a manager', unlock/reset-password tighten it to Owner only, overriding
    permission_classes per-action below. Queryset is scoped to the
    requester's own branch - an owner can't see, edit or unlock another
    branch's staff."""

    queryset = Staff.objects.select_related("branch").prefetch_related("stations").all()
    serializer_class = StaffSerializer
    permission_classes = [IsOwnerOrManager]

    def get_queryset(self):
        qs = super().get_queryset()
        from common.filters import text_filter, choice_filter
        params = self.request.query_params
        qs = text_filter(qs, params, "search", ["name", "username"])
        qs = choice_filter(
            qs, params, "role", field="role",
            allowed=["owner", "manager", "cashier", "waiter", "kitchen"]
        )
        from common.filters import ordering_filter
        return ordering_filter(qs, params, {"name": "name", "role": "role"}, ["name"])

    @action(detail=True, methods=["post"], url_path="unlock", permission_classes=[IsOwner])
    def unlock(self, request, pk=None):
        """Owner only. Clears the lock and the failed-attempt counter and
        records who did it. Optional `new_password` sets a temporary one."""
        staff = self.get_object()
        serializer = TemporaryPasswordSerializer(data=request.data, staff=staff, required=False)
        serializer.is_valid(raise_exception=True)
        if not staff.is_locked:
            return Response({"error": "error.accountNotLocked"}, status=status.HTTP_400_BAD_REQUEST)
        unlock_staff(staff, by=request.user, new_password=serializer.validated_data.get("new_password"))
        return Response(StaffSerializer(staff).data)

    @action(detail=True, methods=["post"], url_path="reset-password", permission_classes=[IsOwner])
    def reset_password(self, request, pk=None):
        """Owner only. Temporary password for someone who forgot theirs; they
        must change it on their next login."""
        staff = self.get_object()
        serializer = TemporaryPasswordSerializer(data=request.data, staff=staff, required=True)
        serializer.is_valid(raise_exception=True)
        reset_password(staff, by=request.user, new_password=serializer.validated_data["new_password"])
        return Response(StaffSerializer(staff).data)

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
