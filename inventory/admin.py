from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction

from common.admin import LIFECYCLE_FIELDSET, SoftDeleteAdmin

from .models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from .services import adjust_stock


@admin.register(InventoryItem)
class InventoryItemAdmin(SoftDeleteAdmin):
    """Manual count changes are recorded as adjustment stock movements."""

    list_display = (
        "id",
        "name",
        "unit",
        "current_stock",
        "par_level",
        "needs_order",
        "cost_per_unit",
        "supplier",
        "branch",
        "is_deleted",
    )
    list_filter = ("branch", "supplier", "unit", "is_deleted")
    search_fields = ("name", "supplier__name_en", "supplier__name_ar")
    list_select_related = ("branch", "supplier")
    autocomplete_fields = ("branch", "supplier")
    fieldsets = (
        (None, {"fields": ("branch", "name", "unit", "supplier")}),
        (
            "Stock",
            {
                "fields": ("current_stock", "par_level", "cost_per_unit"),
                "description": (
                    "Count corrections are recorded as stock adjustments. 'needs order' flags stock at or below par level."
                ),
            },
        ),
        LIFECYCLE_FIELDSET,
    )

    def save_model(self, request, obj, form, change):
        requested_stock = obj.current_stock
        with transaction.atomic():
            if change and obj.pk:
                current = InventoryItem.objects.select_for_update().get(pk=obj.pk)
                obj.current_stock = current.current_stock
                super().save_model(request, obj, form, change)
                delta = requested_stock - current.current_stock
                if delta:
                    updated = adjust_stock(obj.pk, delta, StockMovementReason.ADJUSTMENT)
                    obj.current_stock = updated.current_stock
                return

            if requested_stock < 0:
                raise ValidationError("Initial stock cannot be negative.")
            obj.current_stock = 0
            super().save_model(request, obj, form, change)
            if requested_stock:
                updated = adjust_stock(obj.pk, requested_stock, StockMovementReason.ADJUSTMENT)
                obj.current_stock = updated.current_stock

    @admin.display(boolean=True, description="needs order")
    def needs_order(self, obj):
        """par_level is the reorder trigger, so this is the column the owner
        actually scans for."""
        return obj.current_stock <= obj.par_level


@admin.register(RecipeItem)
class RecipeItemAdmin(SoftDeleteAdmin):
    """One line of a product's recipe: how much of an ingredient one unit uses.
    Deleting an inventory item that appears here is refused (PROTECT), and
    firing a product deducts exactly these quantities x how many were ordered
    (inventory/services.deduct_stock_for_order_item)."""

    list_display = ("id", "product", "inventory_item", "quantity", "unit", "is_deleted")
    list_filter = ("unit", "is_deleted")
    search_fields = ("product__name_en", "product__name_ar", "inventory_item__name")
    list_select_related = ("product", "inventory_item")
    autocomplete_fields = ("product", "inventory_item")


@admin.register(StockMovement)
class StockMovementAdmin(SoftDeleteAdmin):
    """
    Read-only on purpose. The ledger is append-only — the API enforces that by
    simply not exposing update or delete endpoints (inventory/models.py) — and
    a stock ledger you can edit isn't a ledger. Stock changes by *adding* a
    movement (reason=adjustment), never by rewriting one, so this screen exists
    to explain where a number came from.
    """

    list_display = (
        "id", "inventory_item", "previous_stock", "quantity_delta", "resulting_stock",
        "reason", "order", "created_at", "is_deleted",
    )
    list_filter = ("reason", "inventory_item", "is_deleted")
    search_fields = ("inventory_item__name",)
    list_select_related = ("inventory_item", "order")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
