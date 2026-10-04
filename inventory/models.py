from django.core.exceptions import ValidationError
from django.db import models

from branches.models import Branch
from catalog.models import Product
from common.models import BaseModel
from orders.models import Order
from purchasing.models import Supplier


class InventoryItem(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="inventory_items")
    name = models.CharField(max_length=255)
    unit = models.CharField(max_length=32)
    current_stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    par_level = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_items")

    def __str__(self):
        return self.name


class RecipeItem(BaseModel):
    """No separate Recipe model — it would carry no fields beyond product_id
    + a list, so this FK gives the same 1:many shape with one less join
    (same reasoning as catalog's Deal collapsing DealWindow's parent)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="recipe_items")
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="recipe_items")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=32)

    class Meta:
        ordering = ["id"]

    def clean(self):
        super().clean()
        errors = {}
        if self.quantity is None or self.quantity <= 0:
            errors["quantity"] = "Recipe quantity must be greater than zero."
        if not self.unit or not self.unit.strip():
            errors["unit"] = "Recipe unit is required."
        if self.product_id and self.inventory_item_id:
            if self.product.branch_id != self.inventory_item.branch_id:
                errors["inventory_item"] = "Ingredient must belong to the product branch."
            elif not self.unit or not self.unit.strip():
                errors["unit"] = "Recipe unit is required."
            else:
                from .services import units_compatible
                if not units_compatible(self.unit, self.inventory_item.unit):
                    errors["unit"] = "Recipe and inventory units are incompatible."
            duplicates = RecipeItem.objects.filter(
                product_id=self.product_id, inventory_item_id=self.inventory_item_id,
            )
            if self.pk:
                duplicates = duplicates.exclude(pk=self.pk)
            if duplicates.exists():
                errors["inventory_item"] = "An ingredient can appear only once in a recipe."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity}{self.unit} {self.inventory_item.name} for {self.product.name_en}"


class StockMovementReason(models.TextChoices):
    SALE = "sale", "Sale"
    PURCHASE = "purchase", "Purchase"
    WASTE = "waste", "Waste"
    ADJUSTMENT = "adjustment", "Adjustment"


class StockMovement(BaseModel):
    """Append-only ledger with before, delta, and after stock values."""

    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="stock_movements")
    previous_stock = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    quantity_delta = models.DecimalField(max_digits=12, decimal_places=3)
    resulting_stock = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    # Null on pre-costing history: reports must not guess an old cost from
    # today's inventory price.
    unit_cost_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    reason = models.CharField(max_length=16, choices=StockMovementReason.choices)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements")
    order_item = models.ForeignKey(
        "orders.OrderItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements"
    )
    purchase = models.ForeignKey(
        "purchasing.Purchase", on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements"
    )
    purchase_item = models.ForeignKey(
        "purchasing.PurchaseItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements"
    )

    def __str__(self):
        return f"{self.quantity_delta} {self.inventory_item.name} ({self.reason})"
