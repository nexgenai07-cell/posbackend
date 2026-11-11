from rest_framework.exceptions import APIException
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.settings import api_settings

from .models import Staff


class PasswordChangeRequired(APIException):
    """
    403 for a valid token whose owner still has to replace a password someone
    else chose (migration from PINs, a new account, an owner's reset). The
    only views that accept such a token set `allow_password_change_required`
    on the view class: change-password, logout and me.
    """

    status_code = 403
    default_detail = "error.passwordChangeRequired"
    default_code = "password_change_required"


def resolve_staff(staff_id):
    """The one rule for "does this token still open the door": the staff row
    must exist, be active and not be locked. Shared with the WebSocket
    middleware (realtime/middleware.py) and the refresh endpoint."""
    return Staff.objects.filter(pk=staff_id, is_active=True, locked_at__isnull=True).first()


class StaffJWTAuthentication(JWTAuthentication):
    """
    Staff is not AUTH_USER_MODEL (see accounts/models.py), so the default
    User-based lookup doesn't apply - resolve the token's staff-id claim to
    a Staff row instead. A locked staff member's existing tokens stop
    working here, the same way an inactive one's do.
    """

    def get_user(self, validated_token):
        try:
            staff_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError:
            raise InvalidToken("Token contained no recognizable staff identification")

        staff = resolve_staff(staff_id)
        if staff is None:
            raise InvalidToken("Staff not found, inactive or locked")
        return staff

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        staff, _token = result
        if staff.must_change_password:
            # parser_context carries the view DRF is dispatching to.
            view = (getattr(request, "parser_context", None) or {}).get("view")
            if not getattr(view, "allow_password_change_required", False):
                raise PasswordChangeRequired()
        return result
