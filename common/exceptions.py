import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler, set_rollback

logger = logging.getLogger(__name__)

# Framework-level exceptions (auth/permission/404/etc.) don't go through a
# serializer, so they'd otherwise surface DRF's default English "detail"
# text — inconsistent with every validation error in this project being a
# code. Field-level errors already use codes because we write the messages
# ourselves (see catalog/serializers.py etc.); this handler is only for the
# handful of generic exceptions DRF raises on its own.
_CODE_MAP = {
    NotAuthenticated: "error.notAuthenticated",
    AuthenticationFailed: "error.notAuthenticated",
    PermissionDenied: "error.forbidden",
    NotFound: "error.notFound",
    MethodNotAllowed: "error.methodNotAllowed",
    Throttled: "error.tooManyRequests",
}


# DRF's own field messages ("This field is required.", "Ensure this field has
# no more than 100 characters.") are English text, not codes, so the apps
# can't translate them and fell back to "Something went wrong". Each carries
# a machine `code`; map it to one of ours. Our own "error.*" messages pass
# through untouched.
_FIELD_CODE_MAP = {
    "required": "error.fieldRequired",
    "blank": "error.fieldRequired",
    "null": "error.fieldRequired",
    "max_length": "error.fieldTooLong",
    "min_length": "error.fieldTooShort",
    "unique": "error.alreadyExists",
}


def _coded(detail):
    if isinstance(detail, dict):
        return {key: _coded(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return [_coded(value) for value in detail]
    text = str(detail)
    if text.startswith("error."):
        return text
    return _FIELD_CODE_MAP.get(getattr(detail, "code", ""), "error.fieldInvalid")


class ConflictCode(APIException):
    """409 with the project's {"error": "<code>"} body, raisable from inside a
    serializer — used where the same condition is a 409 elsewhere (e.g.
    error.productUnavailable from both add-item validation and dispatch)."""

    status_code = status.HTTP_409_CONFLICT

    def __init__(self, code):
        super().__init__(code)
        self.error_code = code


def error_code_exception_handler(exc, context):
    # DRF's own default handler converts these two Django exceptions to
    # their DRF equivalents, but only in its own local scope — the `exc` we
    # hold here would still be the original Http404/PermissionDenied, so the
    # isinstance checks below would never match without doing this first.
    if isinstance(exc, Http404):
        exc = NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = PermissionDenied()

    response = drf_exception_handler(exc, context)
    if response is None:
        # Not an API error: a bug or an outage behind the view. Log it with the
        # traceback, roll back, and answer in the API's own shape so the apps
        # can say "server error" instead of choking on an HTML 500 page.
        logger.exception("Unhandled error in %s", getattr(context.get("view"), "__class__", type(None)).__name__)
        set_rollback()
        return Response({"error": "error.serverError"}, status=500)

    if isinstance(exc, ValidationError):
        response.data = _coded(exc.detail)
        return response

    if isinstance(exc, ConflictCode):
        response.data = {"error": exc.error_code}
        return response

    for exc_type, code in _CODE_MAP.items():
        if isinstance(exc, exc_type):
            response.data = {"error": code}
            break

    return response
