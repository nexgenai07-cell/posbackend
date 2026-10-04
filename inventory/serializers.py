from rest_framework import serializers

from common.validators import reject_branch_mismatch
from .services import units_compatible

from .models import InventoryItem, RecipeItem, StockMovement, StockMovementReason

MANUAL_STOCK_REASONS = [StockMovementReason.WASTE, StockMovementReason.ADJUSTMENT]


class InventoryItemSerializer(serializers.ModelSerializer):
    name = serializers.CharField(error_messages={"blank": "error.nameRequired", "required": "error.nameRequired"})

    class Meta:
        model = InventoryItem
        fields = [
            "id", "branch", "name", "unit", "current_stock", "par_level",
            "cost_per_unit", "supplier", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_current_stock(self, value):
        if self.instance is None and value < 0:
            raise serializers.ValidationError("error.stockQuantityInvalid")
        return value

    def validate(self, attrs):
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        reject_branch_mismatch(self.context.get("request"), branch)
        return attrs

    def update(self, instance, validated_data):
        # current_stock is settable at creation (initial count) but only
        # ever changes afterward through adjust_stock, to keep every change
        # backed by a StockMovement row — silently ignore it on PATCH/PUT.
        validated_data.pop("current_stock", None)
        return super().update(instance, validated_data)


class StockMovementSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockMovement
        fields = [
            "id", "inventory_item", "previous_stock", "quantity_delta", "resulting_stock",
            "reason", "order", "created_at",
        ]
        read_only_fields = fields


class AdjustStockSerializer(serializers.Serializer):
    """Manual adjustments only (waste/correction) — "sale" is automatic via
    send-to-kitchen, "purchase" via receiving a PO (Phase 9)."""

    quantity_delta = serializers.DecimalField(
        max_digits=12, decimal_places=3,
        error_messages={"required": "error.quantityDeltaRequired", "invalid": "error.quantityDeltaRequired"},
    )
    reason = serializers.ChoiceField(
        choices=MANUAL_STOCK_REASONS,
        error_messages={"invalid_choice": "error.reasonInvalid", "required": "error.reasonInvalid"},
    )

    def validate_quantity_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("error.quantityDeltaRequired")
        return value


class RecipeItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecipeItem
        fields = ["inventory_item", "quantity", "unit"]

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("error.recipeQuantityInvalid")
        return value

    def validate(self, attrs):
        product = self.context.get("product")
        inventory_item = attrs.get("inventory_item", getattr(self.instance, "inventory_item", None))
        unit = attrs.get("unit", getattr(self.instance, "unit", ""))
        if product and inventory_item and product.branch_id != inventory_item.branch_id:
            raise serializers.ValidationError({"inventory_item": "error.inventoryItemInvalid"})
        if not unit or not unit.strip() or (inventory_item and not units_compatible(unit, inventory_item.unit)):
            raise serializers.ValidationError({"unit": "error.recipeUnitInvalid"})
        return attrs


class RecipeRowSerializer(serializers.ModelSerializer):
    """
    Read-only, for the bulk recipe list. Deliberately separate from
    RecipeItemSerializer: that one is the PUT body for a single product's
    recipe (a bare array with no `product` key), so adding `product` to it
    would make every existing recipe save require one.
    """

    class Meta:
        model = RecipeItem
        fields = ["product", "inventory_item", "quantity", "unit"]
        read_only_fields = fields
