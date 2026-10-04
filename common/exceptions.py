from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework.exceptions import (
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
)
from rest_framework.views import exception_handler as drf_exception_handler

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
        return None

    for exc_type, code in _CODE_MAP.items():
        if isinstance(exc, exc_type):
            response.data = {"error": code}
            break

    return response
