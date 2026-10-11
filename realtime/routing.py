from django.urls import re_path

from .consumers import BranchEventsConsumer, PublicMenuEventsConsumer, TableSessionConsumer

websocket_urlpatterns = [
    re_path(r"^ws/events/$", BranchEventsConsumer.as_asgi()),
    re_path(r"^ws/menu/$", PublicMenuEventsConsumer.as_asgi()),
    re_path(r"^ws/table/$", TableSessionConsumer.as_asgi()),
]
