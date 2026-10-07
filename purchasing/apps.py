from django.apps import AppConfig


class PurchasingConfig(AppConfig):
    name = 'purchasing'

    def ready(self):
        from . import signals  # noqa: F401
