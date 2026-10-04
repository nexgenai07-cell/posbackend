"""
Nothing to register here.

This app owns no models: it wires the WebSocket consumers, routing, the JWT
middleware and the model signals that publish change events
(realtime/signals.py). Its effect shows up in the admin only indirectly —
editing a row anywhere else is what fires those signals.
"""
