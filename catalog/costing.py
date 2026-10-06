"""
Recipe costing — ingredient cost, gross profit and food-cost percentage.

Everything here is DERIVED on read from the current
InventoryItem.cost_per_unit. Nothing is stored.

That is deliberate. A stored cost column would have to be invalidated on every
purchase receive, every recipe edit, every unit change and every ingredient
price correction — and the day one of those paths forgets, the margin report
starts lying with no error to notice. Deriving makes "costs update
automatically when a purchase changes the ingredient price" true by
construction instead of by maintenance.

The companion rule, and the one that matters for money:

    LIVE    (here)  recipe cost, food-cost %, gross profit, menu prices
                    -> move the moment an ingredient price moves
    FROZEN  (elsewhere) OrderItem.price_snapshot,
                        StockMovement.unit_cost_snapshot
                    -> never recomputed, so a new chicken purchase can never
                       rewrite what last week's customer was charged

Nothing in this module is allowed to touch a snapshot.
"""

from decimal import Decimal

from inventory.services import required_quantity_in_inventory_unit

_ZERO = Decimal("0.00")
_CENTS = Decimal("0.01")


def recipe_cost(product):
    """
    What one unit of this product's recipe costs at today's ingredient prices.

    Quantities are converted into the inventory item's own unit before being
    multiplied by its cost — a recipe in grams against an ingredient priced per
    KG must not be multiplied raw, or the answer is out by 1000x. That
    conversion is inventory.services' existing helper, reused rather than
    reimplemented so the two can never disagree.

    A recipe line whose units are incompatible contributes 0 rather than
    raising: RecipeItem.clean() already rejects those at write time, so one bad
    legacy row should not take down a product list.
    """
    total = _ZERO
    for item in product.recipe_items.all():
        inventory_item = item.inventory_item
        required = required_quantity_in_inventory_unit(item, inventory_item)
        if required is None:
            continue
        total += required * inventory_item.cost_per_unit
    return total.quantize(_CENTS)


def variant_cost(product, variant=None):
    """
    Ingredient cost for one portion of a specific variant.

    recipe_cost x variant.recipe_multiplier. With chicken at 600/KG and a 1 KG
    recipe: 0.5x -> 300, 1.0x -> 600, 2.0x -> 1200. Raise the ingredient price
    to 750 and all three move on the next read, because none of them was
    stored.
    """
    base = recipe_cost(product)
    if variant is None:
        return base
    return (base * variant.recipe_multiplier).quantize(_CENTS)


def selling_price(product, variant=None):
    """The variant's price, or the product's own when it has no variants."""
    return variant.price if variant is not None else product.price


def costing(product, variant=None):
    """
    The full margin picture for a product or one of its variants.

    `food_cost_percent` is None rather than 0 when there is no selling price —
    a free item has no meaningful percentage, and returning 0 would render as
    a suspiciously healthy margin.

    `has_recipe` lets a UI distinguish "this costs nothing to make" from "no
    recipe has been entered yet", which look identical if you only read the
    numbers.
    """
    ingredient_cost = variant_cost(product, variant)
    price = selling_price(product, variant) or _ZERO
    gross_profit = (price - ingredient_cost).quantize(_CENTS)

    food_cost_percent = None
    if price > 0:
        food_cost_percent = ((ingredient_cost / price) * 100).quantize(_CENTS)

    return {
        "ingredient_cost": ingredient_cost,
        "selling_price": price,
        "gross_profit": gross_profit,
        "food_cost_percent": food_cost_percent,
        "has_recipe": product.recipe_items.exists(),
    }
