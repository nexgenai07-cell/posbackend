from django.db.models.signals import post_delete, post_save
from django.db import transaction
from django.dispatch import receiver

from catalog.models import Deal, Product
from inventory.models import InventoryItem
from orders.models import Order, OrderItem, Payment
from tables.models import Table

from .publisher import publish_event

"""
Signals, not manual publish_event() calls sprinkled through every view: a
model changing is a model changing regardless of which endpoint did it, and
signals fire on every individual .save()/.delete() (including our soft
delete, which is a .save() under the hood) without needing every future
endpoint to remember to broadcast. The one thing signals don't catch is a
bulk queryset .update()/.delete() with no per-instance save — currently the
only such call is SendToKitchenView's pending_items.update(), which is
always paired with an Order.save() in the same request, so the order:updated
event still fires from that. Keep that pairing in mind if a new bulk update
is added elsewhere.
"""


@receiver(post_save, sender=Table)
@receiver(post_delete, sender=Table)
def on_table_change(sender, instance, **kwargs):
    publish_event(instance.branch_id, "table:updated", instance.pk)


@receiver(post_save, sender=Order)
@receiver(post_delete, sender=Order)
def on_order_change(sender, instance, **kwargs):
    publish_event(instance.branch_id, "order:updated", instance.pk, table_id=instance.table_id)


@receiver(post_save, sender=OrderItem)
@receiver(post_delete, sender=OrderItem)
def on_order_item_change(sender, instance, **kwargs):
    publish_event(instance.order.branch_id, "order:updated", instance.order_id, table_id=instance.order.table_id)


@receiver(post_save, sender=Payment)
@receiver(post_delete, sender=Payment)
def on_payment_change(sender, instance, **kwargs):
    publish_event(instance.order.branch_id, "order:updated", instance.order_id, table_id=instance.order.table_id)


@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
def on_product_change(sender, instance, **kwargs):
    publish_event(instance.branch_id, "product:updated", instance.pk)


@receiver(post_save, sender=Deal)
@receiver(post_delete, sender=Deal)
def on_deal_change(sender, instance, **kwargs):
    publish_event(instance.product.branch_id, "product:updated", instance.product_id)


@receiver(post_save, sender=InventoryItem)
@receiver(post_delete, sender=InventoryItem)
def on_inventory_item_change(sender, instance, **kwargs):
    # adjust_stock() always re-saves the InventoryItem (current_stock
    # changes), so this alone covers every stock movement — sale, purchase,
    # waste, manual adjustment — without a separate StockMovement signal.
    branch_id, item_id = instance.branch_id, instance.pk
    transaction.on_commit(lambda: publish_event(branch_id, "inventory:updated", item_id))
