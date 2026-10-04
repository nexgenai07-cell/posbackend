from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def publish_event(branch_id, name, entity_id, table_id=None):
    """
    Broadcasts {"name": name, "payload": {"id": entity_id}} to every
    connected client on this branch — the same shape restaurant-admin's
    lib/eventBus.ts already emits locally, so swapping BroadcastChannel for
    a WebSocket client is the only frontend change this requires.

    When `table_id` is given, the same message is also sent to that table's
    own group (see realtime/consumers.py's TableSessionConsumer) — a
    customer's phone has no staff JWT, so it can't join the branch-wide
    channel above; this narrower one is what Phase 13's restaurant-mobile
    connects to instead, scoped to only the table it scanned.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    if branch_id is not None:
        async_to_sync(channel_layer.group_send)(
            f"branch_{branch_id}_events",
            {"type": "broadcast_event", "name": name, "payload": {"id": entity_id}},
        )
    if table_id is not None:
        async_to_sync(channel_layer.group_send)(
            f"table_{table_id}_events",
            {"type": "broadcast_event", "name": name, "payload": {"id": entity_id}},
        )


def publish_staff_event(staff_id, name, payload):
    """Send a targeted workflow notification through the existing Channels layer."""
    channel_layer = get_channel_layer()
    if channel_layer is None or staff_id is None:
        return
    async_to_sync(channel_layer.group_send)(
        f"staff_{staff_id}_events",
        {"type": "broadcast_event", "name": name, "payload": payload},
    )
