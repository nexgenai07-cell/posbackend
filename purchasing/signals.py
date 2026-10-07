from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from realtime.publisher import publish_event

from .models import Purchase


@receiver(post_save, sender=Purchase)
def publish_purchase_change(sender, instance, **kwargs):
    """Refresh branch purchase summaries on every committed PO change."""
    branch_id, purchase_id = instance.branch_id, instance.pk
    transaction.on_commit(
        lambda: publish_event(branch_id, "inventory:updated", purchase_id)
    )
