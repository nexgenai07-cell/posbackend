from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.settings import api_settings

from .models import Staff


class StaffJWTAuthentication(JWTAuthentication):
    """
    Staff is not AUTH_USER_MODEL (see accounts/models.py), so the default
    User-based lookup doesn't apply — resolve the token's staff-id claim to
    a Staff row instead.
    """

    def get_user(self, validated_token):
        try:
            staff_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError:
            raise InvalidToken("Token contained no recognizable staff identification")

        try:
            return Staff.objects.get(pk=staff_id, is_active=True)
        except Staff.DoesNotExist:
            raise InvalidToken("Staff not found or inactive")
