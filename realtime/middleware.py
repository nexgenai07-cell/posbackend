from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import Staff


@database_sync_to_async
def _get_staff(staff_id):
    try:
        return Staff.objects.get(pk=staff_id, is_active=True)
    except Staff.DoesNotExist:
        return None


class JWTAuthMiddleware(BaseMiddleware):
    """
    A WebSocket handshake can't carry an Authorization header the way a
    browser fetch() can, so the access token travels as a query param
    instead: wss://host/ws/events/?token=<access token>. Not Channels'
    built-in AuthMiddlewareStack — that's for Django session/cookie auth,
    which this project doesn't use (see accounts/authentication.py).
    """

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        token = parse_qs(query_string).get("token", [None])[0]

        scope["staff"] = None
        if token:
            try:
                access_token = AccessToken(token)
                staff_id = access_token[api_settings.USER_ID_CLAIM]
                scope["staff"] = await _get_staff(staff_id)
            except TokenError:
                pass

        return await super().__call__(scope, receive, send)
