from django.db import models

from branches.models import Branch
from common.models import BaseModel


class Supplier(BaseModel):
    """Built in Phase 8 as an InventoryItem.supplier FK dependency, endpoints
    added here in Phase 9."""

    name_en = models.CharField(max_length=255)
    name_ar = models.CharField(max_length=255)
    contact_info = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name_en


class PurchaseStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ORDERED = "ordered", "Ordered"
    RECEIVED = "received", "Received"


class Purchase(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="purchases")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchases")
    status = models.CharField(max_length=16, choices=PurchaseStatus.choices, default=PurchaseStatus.ORDERED)
    ordered_at = models.DateTimeField(auto_now_add=True)
    received_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Purchase #{self.pk} ({self.status})"


class PurchaseItem(BaseModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    # String reference, not a direct import: inventory.models already imports
    # purchasing.models.Supplier, so a direct import here would be circular.
    inventory_item = models.ForeignKey(
        "inventory.InventoryItem", on_delete=models.PROTECT, related_name="purchase_items"
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.inventory_item.name} @ {self.unit_cost}"
