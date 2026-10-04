import json
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer


class BranchEventsConsumer(AsyncWebsocketConsumer):
    """One group per branch. JWTAuthMiddleware (see middleware.py) puts the
    authenticated Staff on scope["staff"] before this ever runs."""

    async def connect(self):
        staff = self.scope.get("staff")
        if staff is None:
            await self.close(code=4001)
            return
        self.group_name = f"branch_{staff.branch_id}_events"
        self.staff_group_name = f"staff_{staff.pk}_events"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.channel_layer.group_add(self.staff_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if hasattr(self, "staff_group_name"):
            await self.channel_layer.group_discard(self.staff_group_name, self.channel_name)

    async def broadcast_event(self, event):
        await self.send(text_data=json.dumps({"name": event["name"], "payload": event["payload"]}))


@database_sync_to_async
def _get_table_id_for_session(raw_token):
    from tables.models import Table
    from tables.services import parse_qr_payload

    # Same normalisation as the REST endpoints (tables/services.py): the
    # customer's phone may hand over the raw "/t/<token>" it scanned, not just
    # a bare token — a "/" is legal in a query string, so
    # ?session=/t/<token> arrives here intact. Unknown/garbage input
    # normalises to "" and resolves to None, i.e. close(4001) as before.
    token = parse_qr_payload(raw_token)
    if not token:
        return None
    return Table.objects.filter(session_token=token).values_list("id", flat=True).first()


class TableSessionConsumer(AsyncWebsocketConsumer):
    """
    One group per table, for a customer's phone (Phase 13) — no staff/JWT
    involved at all, so JWTAuthMiddleware wrapping this alongside
    BranchEventsConsumer is harmless: it just leaves scope["staff"] as None,
    which this consumer ignores. Authenticates instead directly against a
    live table session_token, the same bearer-credential the public REST
    endpoints (TableBySessionView, PublicOrderCreateView) use — see
    tables/models.py's Table.open()/close() for why that token is safe to
    trust this way (CSPRNG, unique, fully rotated on every close).

    wss://host/ws/table/?session=<session_token> — a deliberately different
    query param name than the staff channel's ?token= (a JWT), so the two
    can never be confused even though both ultimately reach the same
    JWTAuthMiddleware first. `session` takes the bare token or the raw
    "/t/<token>" QR payload the phone just scanned, via the same
    tables/services.py's parse_qr_payload() the REST endpoints use.
    """

    async def connect(self):
        query_string = self.scope.get("query_string", b"").decode()
        session_token = parse_qs(query_string).get("session", [None])[0]

        table_id = await _get_table_id_for_session(session_token) if session_token else None
        if table_id is None:
            await self.close(code=4001)
            return

        self.group_name = f"table_{table_id}_events"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def broadcast_event(self, event):
        await self.send(text_data=json.dumps({"name": event["name"], "payload": event["payload"]}))
