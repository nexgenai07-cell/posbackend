"""
Password strength, in one place for the API serializers, the Django admin
form and the unlock_staff management command.

The rules themselves are Django's own validators (config/settings.py
AUTH_PASSWORD_VALIDATORS): at least 8 characters, not entirely numeric, not
a common password, and not too similar to the username or name. Their
English messages are translated into this API's error codes so the apps
can show them in the staff member's language.
"""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

_ERROR_CODES = {
    "password_too_short": "error.passwordTooShort",
    "password_too_common": "error.passwordTooCommon",
    "password_entirely_numeric": "error.passwordNumeric",
    "password_too_similar": "error.passwordTooSimilar",
}


def password_error_codes(raw_password, staff=None):
    """Every rule the password breaks, as error codes; empty when it's fine.
    `staff` (may be unsaved) feeds the similarity check against username/name."""
    try:
        validate_password(raw_password, user=staff)
    except DjangoValidationError as exc:
        return [_ERROR_CODES.get(error.code, "error.passwordInvalid") for error in exc.error_list]
    return []


def validate_new_password(raw_password, staff=None, field="password"):
    """Serializer-side check: raises on the first broken rule, keyed by `field`."""
    codes = password_error_codes(raw_password, staff)
    if codes:
        raise serializers.ValidationError({field: codes[0]})
