from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from catalog.services import is_available_today
from .models import InventoryItem, RecipeItem, StockMovement, StockMovementReason


_UNIT_FACTORS = {
    "mg": ("mass", Decimal("0.001")),
    "g": ("mass", Decimal("1")),
    "gram": ("mass", Decimal("1")),
    "grams": ("mass", Decimal("1")),
    "kg": ("mass", Decimal("1000")),
    "kilogram": ("mass", Decimal("1000")),
    "kilograms": ("mass", Decimal("1000")),
    "ml": ("volume", Decimal("1")),
    "milliliter": ("volume", Decimal("1")),
    "milliliters": ("volume", Decimal("1")),
    "l": ("volume", Decimal("1000")),
    "liter": ("volume", Decimal("1000")),
    "liters": ("volume", Decimal("1000")),
    "litre": ("volume", Decimal("1000")),
    "litres": ("volume", Decimal("1000")),
    "piece": ("count", Decimal("1")),
    "pieces": ("count", Decimal("1")),
    "pc": ("count", Decimal("1")),
    "pcs": ("count", Decimal("1")),
    "unit": ("count", Decimal("1")),
    "units": ("count", Decimal("1")),
    "dozen": ("count", Decimal("12")),
}


class ProductStockUnavailable(Exception):
    def __init__(self, reason, inventory_item_ids=()):
        self.reason = reason
        self.inventory_item_ids = tuple(inventory_item_ids)
        super().__init__(reason)


def _required_in_inventory_unit(recipe_item, inventory_item):
    recipe_unit = recipe_item.unit.strip().casefold()
    stock_unit = inventory_item.unit.strip().casefold()
    recipe_factor = _UNIT_FACTORS.get(recipe_unit)
    stock_factor = _UNIT_FACTORS.get(stock_unit)
    if recipe_factor or stock_factor:
        if not recipe_factor or not stock_factor or recipe_factor[0] != stock_factor[0]:
            return None
        return recipe_item.quantity * recipe_factor[1] / stock_factor[1]
    # Custom units are supported only when they match exactly.
    if recipe_unit and recipe_unit == stock_unit:
        return recipe_item.quantity
    return None


def required_quantity_in_inventory_unit(recipe_item, inventory_item):
    """Convert a recipe quantity to stock units, or return None if incompatible."""
    return _required_in_inventory_unit(recipe_item, inventory_item)


def latest_actual_purchase_cost(inventory_item_id):
    """Latest received PO line cost, or None when no actual purchase is recorded."""
    from purchasing.models import PurchaseItem, PurchaseStatus

    return PurchaseItem.objects.filter(
        inventory_item_id=inventory_item_id,
        purchase__status=PurchaseStatus.RECEIVED,
    ).order_by("-purchase__received_at", "-id").values_list("unit_cost", flat=True).first()


def units_compatible(recipe_unit, stock_unit):
    recipe_unit = (recipe_unit or "").strip().casefold()
    stock_unit = (stock_unit or "").strip().casefold()
    recipe_factor = _UNIT_FACTORS.get(recipe_unit)
    stock_factor = _UNIT_FACTORS.get(stock_unit)
    if recipe_factor or stock_factor:
        return bool(recipe_factor and stock_factor and recipe_factor[0] == stock_factor[0])
    return bool(recipe_unit and recipe_unit == stock_unit)


def check_products_stock(products, quantity=1, *, lock=False):
    """Return availability by product using shared recipe and stock rules."""
    products = list(products)
    if not products:
        return {}
    quantity = Decimal(str(quantity))
    product_by_id = {product.pk: product for product in products}
    recipe = list(
        RecipeItem.objects.filter(product_id__in=product_by_id)
        .select_related("product", "inventory_item")
        .order_by("product_id", "inventory_item_id", "id")
    )
    recipe_by_product = defaultdict(list)
    inventory_ids = set()
    for line in recipe:
        recipe_by_product[line.product_id].append(line)
        inventory_ids.add(line.inventory_item_id)
    inventory = InventoryItem.objects.filter(pk__in=inventory_ids)
    if lock:
        inventory = inventory.select_for_update()
    items = {item.pk: item for item in inventory.order_by("pk")}
    availability = {}
    for product_id, product in product_by_id.items():
        lines = recipe_by_product.get(product_id, [])
        if not lines:
            availability[product_id] = (False, "missing_recipe")
            continue
        required_by_item = defaultdict(Decimal)
        seen_ingredients = set()
        invalid = False
        for line in lines:
            stock_item = items.get(line.inventory_item_id)
            if (line.quantity <= 0 or line.product_id != product.pk or not line.unit
                    or stock_item is None or stock_item.branch_id != product.branch_id
                    or line.inventory_item_id in seen_ingredients):
                invalid = True
                break
            seen_ingredients.add(line.inventory_item_id)
            required = _required_in_inventory_unit(line, stock_item)
            if required is None or required <= 0:
                invalid = True
                break
            required_by_item[stock_item.pk] += required * quantity
        if invalid:
            availability[product_id] = (False, "invalid_recipe")
        elif any(items[item_id].current_stock <= 0 or items[item_id].current_stock < required
                 for item_id, required in required_by_item.items()):
            availability[product_id] = (False, "insufficient_stock")
        else:
            availability[product_id] = (True, None)
    return availability


def check_product_stock(product, quantity=1, *, lock=False):
    """Check a product's complete recipe with common units and branch safety."""
    return check_products_stock([product], quantity, lock=lock)[product.pk]


def require_product_stock(product, quantity=1, *, lock=False):
    available, reason = check_product_stock(product, quantity, lock=lock)
    if not available:
        return False, reason
    return True, None


def adjust_stock(
    inventory_item_id, quantity_delta, reason, order=None, *, order_item=None, purchase=None, purchase_item=None,
    unit_cost_snapshot=None,
):
    """
    The only place stock ever changes. select_for_update() locks this row
    for the rest of the transaction, so two concurrent callers touching the
    same ingredient (e.g. two orders both needing the last of the buns)
    serialize instead of both reading the same stale current_stock and
    overselling. Must be called from inside a transaction.atomic() block —
    the lock is meaningless without one.
    """
    quantity_delta = Decimal(str(quantity_delta))
    item = InventoryItem.objects.select_for_update().get(pk=inventory_item_id)
    previous_stock = item.current_stock
    resulting_stock = previous_stock + quantity_delta
    if resulting_stock < 0:
        raise ValidationError("error.stockAdjustmentBelowZero")
    item.current_stock = resulting_stock
    item.save(update_fields=["current_stock", "updated_at"])
    StockMovement.objects.create(
        inventory_item=item,
        previous_stock=previous_stock,
        quantity_delta=quantity_delta,
        resulting_stock=item.current_stock,
        unit_cost_snapshot=(
            latest_actual_purchase_cost(item.pk)
            if unit_cost_snapshot is None
            else Decimal(str(unit_cost_snapshot))
        ),
        reason=reason,
        order=order,
        order_item=order_item,
        purchase=purchase,
        purchase_item=purchase_item,
    )
    return item


def deduct_stock_for_order_item(order_item):
    """Compatibility wrapper for callers that dispatch one line at a time."""
    deduct_stock_for_order_items([order_item])


def deduct_stock_for_order_items(order_items):
    """Lock all recipe ingredients in a stable order, validate totals, deduct.

    Aggregating across the whole dispatch prevents duplicate product lines or
    different products sharing an ingredient from overselling within one order.
    """
    per_line = {}
    total_required = defaultdict(Decimal)
    inventory_branch = {}
    for order_item in order_items:
        product = order_item.product
        if product.branch_id != order_item.order.branch_id:
            raise ProductStockUnavailable("invalid_recipe")
        if not is_available_today(product):
            raise ProductStockUnavailable("product_unavailable")
        recipe = list(
            product.recipe_items.select_related("inventory_item").order_by("inventory_item_id")
        )
        if not recipe:
            raise ProductStockUnavailable("missing_recipe")
        line_required = defaultdict(Decimal)
        seen_ingredients = set()
        for recipe_item in recipe:
            inventory_item = recipe_item.inventory_item
            if (recipe_item.quantity <= 0 or inventory_item.branch_id != product.branch_id
                    or not units_compatible(recipe_item.unit, inventory_item.unit)
                    or inventory_item.pk in seen_ingredients):
                raise ProductStockUnavailable("invalid_recipe")
            seen_ingredients.add(inventory_item.pk)
            amount = _required_in_inventory_unit(recipe_item, inventory_item) * order_item.quantity
            if amount <= 0:
                raise ProductStockUnavailable("invalid_recipe")
            line_required[inventory_item.pk] += amount
            total_required[inventory_item.pk] += amount
            inventory_branch[inventory_item.pk] = product.branch_id
        per_line[order_item.pk] = (order_item, line_required)

    if not total_required:
        raise ProductStockUnavailable("missing_recipe")
    locked_items = {
        item.pk: item for item in InventoryItem.objects.select_for_update()
        .filter(pk__in=total_required, branch_id__in=set(inventory_branch.values())).order_by("pk")
    }
    if len(locked_items) != len(total_required) or any(
        locked_items[item_id].branch_id != inventory_branch[item_id] for item_id in total_required
    ):
        raise ProductStockUnavailable("invalid_recipe")
    if any(locked_items[item_id].current_stock < required or locked_items[item_id].current_stock <= 0
           for item_id, required in total_required.items()):
        raise ProductStockUnavailable("insufficient_stock")

    for _item_id, (order_item, requirements) in sorted(per_line.items()):
        for inventory_item_id, quantity in sorted(requirements.items()):
            adjust_stock(
                inventory_item_id,
                -quantity,
                StockMovementReason.SALE,
                order=order_item.order,
                order_item=order_item,
            )
