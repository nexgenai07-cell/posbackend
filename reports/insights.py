"""
The owner-facing judgements the reports make, in one place so every screen
(and every client) applies the same rule: what counts as a low or good
margin, when a dish's ingredients have got dearer enough to re-price it, and
which things the business summary flags for attention. The admin only
translates and formats what comes out of here; it decides nothing itself.
"""
from decimal import Decimal

# Share of the selling price left after ingredients.
LOW_MARGIN_PCT = Decimal("30")
GOOD_MARGIN_PCT = Decimal("55")
# Today's recipe cost per plate this much above what the sold plates cost.
COST_RISE = Decimal("0.10")

BANDS = {"low_margin_pct": LOW_MARGIN_PCT, "good_margin_pct": GOOD_MARGIN_PCT, "cost_rise_pct": COST_RISE * 100}

NEEDS_ATTENTION = {"loss", "low", "no_recipe"}


def margin_verdict(margin_pct):
    """loss / low / ok / good, or no_recipe when the cost is unknown."""
    if margin_pct is None:
        return "no_recipe"
    if margin_pct < 0:
        return "loss"
    if margin_pct < LOW_MARGIN_PCT:
        return "low"
    if margin_pct < GOOD_MARGIN_PCT:
        return "ok"
    return "good"


def cost_rose(cost_per_plate, current_cost_per_plate):
    """True when one plate costs noticeably more to make today than it did when sold."""
    if cost_per_plate is None or current_cost_per_plate is None or cost_per_plate <= 0:
        return False
    return current_cost_per_plate > cost_per_plate * (1 + COST_RISE)


def _dish(row):
    return {"name_en": row["product_name"], "name_ar": row["product_name_ar"],
            "variant_en": row["variant_name"], "variant_ar": row["variant_name_ar"]}


def attention_items(dishes, low_stock, waste, cancelled):
    """
    The business summary's "needs your attention" list, most urgent first.
    Each item is a code for the admin to translate, the figures it quotes,
    and where to go to act on it.
    """
    items = []
    no_recipe = [row for row in dishes if row["verdict"] == "no_recipe"]
    if no_recipe:
        items.append({"code": "no_recipe", "tone": "warn", "link": "recipes",
                      "count": len(no_recipe), "dishes": [_dish(row) for row in no_recipe]})
    for row in dishes:
        if row["verdict"] in ("loss", "low"):
            items.append({
                "code": row["verdict"], "tone": "danger" if row["verdict"] == "loss" else "warn", "link": "dish_profit",
                "dish": _dish(row), "price": row["average_selling_price"], "cost": row["cost_per_plate"],
                "profit": row["gross_profit_per_unit"], "margin_pct": row["gross_margin_pct"],
            })
    for row in dishes:
        if row["cost_rose"]:
            items.append({
                "code": "cost_up", "tone": "info", "link": "dish_profit", "dish": _dish(row),
                "then": row["cost_per_plate"], "now": row["current_recipe_cost_per_unit"],
            })
    if cancelled["count"]:
        items.append({"code": "cancelled", "tone": "danger" if cancelled["refunded"] else "warn", "link": "sales", **cancelled})
    if low_stock:
        items.append({"code": "low_stock", "tone": "warn", "link": "stock", "count": len(low_stock), "items": low_stock})
    if waste["rows"] and waste["total"] > 0:
        worst = waste["rows"][0]
        items.append({"code": "waste", "tone": "info", "link": "wastage", "total": waste["total"],
                      "item": worst["inventory_item_name"], "amount": worst["estimated_cost"]})
    if waste["unpriced_items"]:
        items.append({"code": "waste_unpriced", "tone": "info", "link": "wastage", "count": waste["unpriced_items"]})
    return items
