"""
Reversing and re-applying the stock impact of a received purchase order.

Editing or deleting a PO that has already been received is only safe if the
inventory it added comes back out. The rule this module follows:

    NEVER delete a StockMovement. Write a COMPENSATING one.

StockMovement is an append-only ledger (see inventory/models.py) — it is what
every historical food-cost and stock report reads. Deleting rows would rewrite
history and make yesterday's reports disagree with themselves. A reversal is a
new negative movement, so the ledger keeps saying "we received 20, then took 20
back out", which is the truth.

Ingredient cost on reversal (the project's chosen rule): restore the cost from
the PREVIOUS purchase of that ingredient, read back out of the ledger. No
weighted averaging — the cost reverts to what it actually was before this PO
landed.
"""

import logging
from decimal import Decimal

from django.db import transaction

from inventory.models import InventoryItem, StockMovement, StockMovementReason
from inventory.services import adjust_stock

logger = logging.getLogger("purchasing.services")


def previous_cost_for(inventory_item_id, exclude_purchase_id):
    """
    What this ingredient cost before the given purchase was received.

    Read from the ledger's own unit_cost_snapshot rather than recomputed, so
    the restored figure is exactly what was previously paid. Returns None when
    there is no earlier purchase — the caller then leaves the current cost
    alone rather than inventing a zero, which would make every recipe using
    that ingredient look free.
    """
    movement = (
        StockMovement.objects.filter(
            inventory_item_id=inventory_item_id,
            reason=StockMovementReason.PURCHASE,
            unit_cost_snapshot__isnull=False,
        )
        .exclude(purchase_id=exclude_purchase_id)
        .order_by("-created_at", "-id")
        .first()
    )
    return movement.unit_cost_snapshot if movement else None


def reverse_purchase_stock(purchase, actor=None):
    """
    Take back out everything this purchase put in, and restore ingredient costs.

    Caller must already be inside transaction.atomic() — adjust_stock() takes a
    row lock per ingredient and that lock is meaningless without one.

    Reverses the quantity ACTUALLY RECEIVED, read from the ledger, not the
    quantity currently on the PO lines. Those differ precisely in the case this
    exists for: someone edited the PO after it was received.
    """
    movements = list(
        StockMovement.objects.filter(
            purchase=purchase, reason=StockMovementReason.PURCHASE,
        ).select_related("inventory_item", "purchase_item").order_by("id")
    )
    net_by_inventory = {}
    latest_positive_by_inventory = {}
    for movement in movements:
        net_by_inventory[movement.inventory_item_id] = (
            net_by_inventory.get(movement.inventory_item_id, Decimal("0")) + movement.quantity_delta
        )
        if movement.quantity_delta > 0:
            latest_positive_by_inventory[movement.inventory_item_id] = movement

    reversed_lines = 0
    for inventory_item_id, net_quantity in net_by_inventory.items():
        # Only reverse the still-applied balance. Earlier positive movements
        # may already have matching compensating negatives from prior edits.
        if net_quantity <= 0:
            continue
        movement = latest_positive_by_inventory[inventory_item_id]
        adjust_stock(
            inventory_item_id,
            -net_quantity,
            StockMovementReason.PURCHASE,
            purchase=purchase,
            purchase_item=movement.purchase_item,
            unit_cost_snapshot=movement.unit_cost_snapshot,
        )
        restored = previous_cost_for(inventory_item_id, purchase.pk)
        if restored is not None:
            InventoryItem.objects.filter(pk=inventory_item_id).update(cost_per_unit=restored)
        reversed_lines += 1

    logger.info(
        "Reversed %s received line(s) for purchase %s (actor=%s)",
        reversed_lines, purchase.pk, getattr(actor, "pk", None),
    )
    return reversed_lines


def apply_purchase_stock(purchase, actor=None):
    """
    Add this purchase's current lines to stock and refresh ingredient costs.

    The same work PurchaseViewSet.receive() does, factored out so receiving and
    re-receiving-after-an-edit cannot drift apart.
    """
    for purchase_item in purchase.items.select_related("inventory_item"):
        adjust_stock(
            purchase_item.inventory_item_id,
            purchase_item.quantity,
            StockMovementReason.PURCHASE,
            purchase=purchase,
            purchase_item=purchase_item,
            unit_cost_snapshot=purchase_item.unit_cost,
        )
        InventoryItem.objects.filter(pk=purchase_item.inventory_item_id).update(
            cost_per_unit=purchase_item.unit_cost
        )
    logger.info("Applied purchase %s to stock (actor=%s)", purchase.pk, getattr(actor, "pk", None))


def net_received_quantity(purchase, inventory_item_id=None):
    """
    What this purchase has NET added to stock, after any reversals.

    Zero means every received line has been taken back out — used to confirm a
    reversal actually balanced rather than trusting that it did.
    """
    movements = StockMovement.objects.filter(purchase=purchase, reason=StockMovementReason.PURCHASE)
    if inventory_item_id is not None:
        movements = movements.filter(inventory_item_id=inventory_item_id)
    return sum((movement.quantity_delta for movement in movements), Decimal("0"))
