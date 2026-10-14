"""
Bulk import of stock, menu and recipes from the admin's Excel template.

Every importer works in two passes over the same uploaded file:

  plan()  reads each row, validates it against the database and records what
          WOULD happen — create / update (with every old -> new value) /
          unchanged / delete / error — without writing anything. This is the
          preview the admin reviews.
  apply() performs exactly that plan inside one transaction. The view only
          calls it when plan() found no errors, so an import either lands
          completely or not at all.

Matching is by name (case-insensitive), the same rule the manual forms use to
reject duplicates. Rows left out of the file are never deleted — except a
recipe, which is replaced as a whole for each product the file lists, and
each removed ingredient line is shown in the preview as a delete.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from catalog.models import Category, Product, ProductVariant
from inventory.models import InventoryItem, RecipeItem, StockMovementReason
from inventory.services import adjust_stock, units_compatible
from purchasing.models import Supplier

from .workbook import Column, decimal, show, text, yes_no

UNITS = ["kg", "g", "l", "ml", "pcs", "dozen"]


@dataclass
class RowResult:
    row: int | None  # Excel row number; None for a recipe line the file removes
    name: str
    action: str = "unchanged"
    changes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def change(self, key, old, new):
        self.changes.append({"field": key, "old": show(old), "new": show(new)})

    def error(self, code, **params):
        self.errors.append({"code": code, "params": params})

    def as_dict(self):
        action = "error" if self.errors else self.action
        return {
            "row": self.row, "name": self.name, "action": action,
            "changes": self.changes, "errors": self.errors, "notes": self.notes,
        }


def _key(value):
    return text(value).casefold()


def _limit(result, value, column, max_length):
    if len(value) > max_length:
        result.error("tooLong", column=column.header, max=max_length)


class Importer:
    kind = ""
    sheet_title = ""
    columns: list[Column] = []
    instructions: list[str] = []

    def __init__(self, branch):
        self.branch = branch

    def col(self, key):
        return next(column for column in self.columns if column.key == key)

    def template_rows(self):
        raise NotImplementedError

    def lists(self):
        raise NotImplementedError

    def plan(self, rows):
        raise NotImplementedError

    def apply(self):
        raise NotImplementedError


COMMON_STEPS = [
    "1. Change the rows on the first sheet, or add new rows under them. Keep the header row as it is.",
    "2. Save the file, then upload it on the same admin page.",
    "3. You will see every change before anything is saved. Nothing is saved until you press Confirm.",
]


# ── Stock ────────────────────────────────────────────────────────────────────

class StockImporter(Importer):
    kind = "stock"
    sheet_title = "Stock"
    columns = [
        Column("name", "Name", "The ingredient or item. A name that already exists updates that item; a new name adds it.", required=True, width=28, example="Chicken"),
        Column("unit", "Unit", "How you count it: kg, g, l, ml, pcs or dozen.", required=True, width=10, example="kg", choices="Units"),
        Column("current_stock", "Current stock", "How much you have right now, in the unit above. Leave empty to keep the current amount. A change is saved as a stock adjustment.", example="25"),
        Column("par_level", "Par level", "Warn me when stock falls to this amount. Leave empty to keep the current level.", example="5"),
        Column("cost_per_unit", "Cost per unit", "What you pay for ONE unit (one kg, one piece...). Leave empty to keep the current cost.", width=16, example="650"),
        Column("supplier", "Supplier", "Optional. A supplier name that does not exist yet is added to Suppliers.", width=26, example="Al Noor Poultry", choices="Suppliers"),
    ]
    instructions = ["Stock import", *COMMON_STEPS, "Removing a row from this file does NOT delete the item from the system."]

    def _items(self):
        return InventoryItem.objects.filter(branch=self.branch).select_related("supplier").order_by("name")

    def template_rows(self):
        return [{
            "name": item.name, "unit": item.unit, "current_stock": item.current_stock,
            "par_level": item.par_level, "cost_per_unit": item.cost_per_unit,
            "supplier": item.supplier.name_en if item.supplier else "",
        } for item in self._items()]

    def lists(self):
        return {"Units": UNITS, "Suppliers": sorted(s.name_en for s in Supplier.objects.all() if s.name_en)}

    def plan(self, rows):
        existing = {item.name.strip().casefold(): item for item in self._items().prefetch_related("recipe_items__product")}
        suppliers = {}
        for supplier in Supplier.objects.all():
            for name in (supplier.name_en, supplier.name_ar):
                if name:
                    suppliers.setdefault(name.strip().casefold(), supplier)
        self._new_suppliers = {}
        self._ops = []
        seen = {}
        results = []
        for row in rows:
            values = row.values
            name = text(values.get("name"))
            result = RowResult(row.number, name)
            results.append(result)
            if not name:
                result.error("required", column=self.col("name").header)
            _limit(result, name, self.col("name"), 255)
            if name and _key(name) in seen:
                result.error("duplicateInFile", firstRow=seen[_key(name)])
            elif name:
                seen[_key(name)] = row.number
            unit = text(values.get("unit"))
            if not unit:
                result.error("required", column=self.col("unit").header)
            _limit(result, unit, self.col("unit"), 32)
            stock, problem = decimal(values.get("current_stock"), self.col("current_stock"))
            if problem:
                result.errors.append(problem)
            par, problem = decimal(values.get("par_level"), self.col("par_level"))
            if problem:
                result.errors.append(problem)
            cost, problem = decimal(values.get("cost_per_unit"), self.col("cost_per_unit"))
            if problem:
                result.errors.append(problem)
            supplier_name = text(values.get("supplier"))
            _limit(result, supplier_name, self.col("supplier"), 255)
            if result.errors:
                continue

            supplier = None
            if supplier_name:
                supplier = suppliers.get(_key(supplier_name))
                if supplier is None:
                    self._new_suppliers.setdefault(_key(supplier_name), supplier_name)
                    result.notes.append({"code": "newSupplier", "params": {"name": supplier_name}})
            item = existing.get(_key(name))
            if item is None:
                result.action = "create"
                result.change("unit", "", unit)
                for key, value in (("current_stock", stock), ("par_level", par), ("cost_per_unit", cost)):
                    if value is not None:
                        result.change(key, "", value)
                if supplier_name:
                    result.change("supplier", "", supplier_name)
                self._ops.append(("create", name, unit, stock or Decimal("0"), par or Decimal("0"), cost or Decimal("0"), supplier, supplier_name))
                continue

            fields = {}
            if unit != item.unit:
                broken = sorted({line.product.name_en or line.product.name_ar for line in item.recipe_items.all() if not units_compatible(line.unit, unit)})
                if broken:
                    result.error("unitBreaksRecipes", unit=unit, products=", ".join(broken))
                    continue
                fields["unit"] = unit
                result.change("unit", item.unit, unit)
            for key, value in (("par_level", par), ("cost_per_unit", cost)):
                if value is not None and value != getattr(item, key):
                    fields[key] = value
                    result.change(key, getattr(item, key), value)
            current_supplier = item.supplier.name_en if item.supplier else ""
            if supplier_name and _key(supplier_name) != _key(current_supplier):
                result.change("supplier", current_supplier, supplier_name)
                fields["supplier"] = (supplier, supplier_name)
            delta = Decimal("0")
            if stock is not None and stock != item.current_stock:
                delta = stock - item.current_stock
                result.change("current_stock", item.current_stock, stock)
            if fields or delta:
                result.action = "update"
                self._ops.append(("update", item, fields, delta))
        return results

    def _supplier(self, supplier, name, created):
        if supplier is not None:
            return supplier
        key = _key(name)
        if key not in created:
            created[key] = Supplier.objects.create(name_en=self._new_suppliers[key], name_ar="")
        return created[key]

    def apply(self):
        created = {}
        for op in self._ops:
            if op[0] == "create":
                _, name, unit, stock, par, cost, supplier, supplier_name = op
                item = InventoryItem.objects.create(
                    branch=self.branch, name=name, unit=unit, current_stock=0, par_level=par, cost_per_unit=cost,
                    supplier=self._supplier(supplier, supplier_name, created) if supplier_name else None,
                )
                if stock:
                    adjust_stock(item.pk, stock, StockMovementReason.ADJUSTMENT)
            else:
                _, item, fields, delta = op
                if "supplier" in fields:
                    supplier, supplier_name = fields["supplier"]
                    fields["supplier"] = self._supplier(supplier, supplier_name, created)
                for key, value in fields.items():
                    setattr(item, key, value)
                if fields:
                    item.save(update_fields=[*fields, "updated_at"])
                if delta:
                    # Every stock change stays backed by a StockMovement row,
                    # exactly like a manual adjustment from the Stock page.
                    adjust_stock(item.pk, delta, StockMovementReason.ADJUSTMENT)


# ── Menu (products and their sizes) ─────────────────────────────────────────

PRODUCT_FIELDS = ("category", "name_en", "name_ar", "description_en", "description_ar", "price", "available")


class ProductImporter(Importer):
    kind = "products"
    sheet_title = "Menu"
    columns = [
        Column("category", "Category", "Menu section, e.g. Burgers. A category that does not exist yet is added.", required=True, width=20, example="Karahi", choices="Categories"),
        Column("name_en", "Name (English)", "Dish name in English. English or Arabic name is required.", width=28, example="Chicken Karahi"),
        Column("name_ar", "Name (Arabic)", "Dish name in Arabic (optional if the English name is filled).", width=24, example="كراهي دجاج"),
        Column("description_en", "Description (English)", "Optional.", width=32),
        Column("description_ar", "Description (Arabic)", "Optional.", width=32),
        Column("price", "Price", "Menu price. Required for a new dish. For a dish with sizes, the price of the regular size.", width=10, example="1400"),
        Column("available", "Available", "Yes or No. Leave empty to keep it as it is (new dishes are available).", width=11, example="Yes", choices="YesNo"),
        Column("size_en", "Size (English)", "Only for dishes sold in sizes: one row per size, repeating the dish name. e.g. Half KG / 1 KG.", width=16, example="1 KG"),
        Column("size_ar", "Size (Arabic)", "Optional Arabic size name.", width=14),
        Column("size_price", "Size price", "Price of this size. Required for a new size.", width=11, example="2600"),
        Column("recipe_multiplier", "Recipe multiplier", "How many times the dish's recipe this size uses: Half KG = 1, 1 KG = 2. Empty = 1 for a new size.", width=17, example="2"),
    ]
    instructions = [
        "Menu import", *COMMON_STEPS,
        "A dish with sizes takes one row per size. Repeat the dish's name, category and price on each of its rows.",
        "Removing a row from this file does NOT delete the dish or size from the system.",
    ]

    def _products(self):
        return Product.objects.filter(branch=self.branch).select_related("category").prefetch_related("variants").order_by("category__sort_order", "category_id", "name_en")

    def template_rows(self):
        rows = []
        for product in self._products():
            base = {
                "category": product.category.name_en or product.category.name_ar,
                "name_en": product.name_en, "name_ar": product.name_ar,
                "description_en": product.description_en, "description_ar": product.description_ar,
                "price": product.price, "available": "Yes" if product.is_available else "No",
            }
            variants = list(product.variants.all())
            if not variants:
                rows.append(base)
            for variant in variants:
                rows.append({**base, "size_en": variant.name_en, "size_ar": variant.name_ar,
                             "size_price": variant.price, "recipe_multiplier": variant.recipe_multiplier})
        return rows

    def lists(self):
        categories = Category.objects.filter(branch=self.branch)
        return {"Categories": [c.name_en or c.name_ar for c in categories], "YesNo": ["Yes", "No"]}

    def _read(self, row, result):
        values = row.values
        parsed = {key: text(values.get(key)) for key in ("category", "name_en", "name_ar", "description_en", "description_ar", "size_en", "size_ar")}
        for key, max_length in (("category", 255), ("name_en", 255), ("name_ar", 255), ("size_en", 120), ("size_ar", 120)):
            _limit(result, parsed[key], self.col(key), max_length)
        for key in ("price", "size_price"):
            parsed[key], problem = decimal(values.get(key), self.col(key))
            if problem:
                result.errors.append(problem)
        parsed["recipe_multiplier"], problem = decimal(values.get("recipe_multiplier"), self.col("recipe_multiplier"), minimum=Decimal("0.001"), allow_zero=False)
        if problem:
            result.errors.append(problem)
        parsed["available"], problem = yes_no(values.get("available"), self.col("available"))
        if problem:
            result.errors.append(problem)
        return parsed

    def plan(self, rows):
        by_name = {}
        for product in self._products():
            for name in (product.name_en, product.name_ar):
                if name:
                    by_name.setdefault(name.strip().casefold(), product)
        categories = {}
        for category in Category.objects.filter(branch=self.branch):
            for name in (category.name_en, category.name_ar):
                if name:
                    categories.setdefault(name.strip().casefold(), category)

        groups = {}
        results = []
        for row in rows:
            parsed_preview = text(row.values.get("name_en")) or text(row.values.get("name_ar"))
            size_preview = text(row.values.get("size_en")) or text(row.values.get("size_ar"))
            result = RowResult(row.number, f"{parsed_preview} · {size_preview}" if size_preview else parsed_preview)
            results.append(result)
            parsed = self._read(row, result)
            if not parsed["name_en"] and not parsed["name_ar"]:
                result.error("nameRequired")
            if not parsed["category"]:
                result.error("required", column=self.col("category").header)
            has_size_values = parsed["size_price"] is not None or parsed["recipe_multiplier"] is not None
            if has_size_values and not (parsed["size_en"] or parsed["size_ar"]):
                result.error("sizeNameRequired")
            if not (parsed["name_en"] or parsed["name_ar"]):
                continue
            key = _key(parsed["name_en"] or parsed["name_ar"])
            group = groups.get(key)
            if group is None:
                group = groups[key] = {"first": row.number, "values": parsed, "rows": [], "sizes": {}}
            else:
                for field_key in PRODUCT_FIELDS:
                    mine, theirs = parsed[field_key], group["values"][field_key]
                    if mine not in (None, "") and theirs not in (None, "") and (_key(mine) if isinstance(mine, str) else mine) != (_key(theirs) if isinstance(theirs, str) else theirs):
                        result.error("conflictingValue", column=self.col(field_key).header, firstRow=group["first"])
                    elif mine not in (None, "") and theirs in (None, ""):
                        group["values"][field_key] = mine
            size_name = parsed["size_en"] or parsed["size_ar"]
            if size_name:
                if _key(size_name) in group["sizes"]:
                    result.error("duplicateInFile", firstRow=group["sizes"][_key(size_name)])
                else:
                    group["sizes"][_key(size_name)] = row.number
            elif any(not entry[1]["size_en"] and not entry[1]["size_ar"] for entry in group["rows"]):
                result.error("duplicateInFile", firstRow=group["first"])
            group["rows"].append((result, parsed))

        self._ops = []
        self._new_categories = {}
        for group in groups.values():
            if any(result.errors for result, _ in group["rows"]):
                continue
            values = group["values"]
            first_result = group["rows"][0][0]
            product = by_name.get(_key(values["name_en"])) if values["name_en"] else None
            product = product or (by_name.get(_key(values["name_ar"])) if values["name_ar"] else None)

            category = categories.get(_key(values["category"]))
            if category is None:
                self._new_categories.setdefault(_key(values["category"]), values["category"])
                first_result.notes.append({"code": "newCategory", "params": {"name": values["category"]}})

            product_fields = {}
            if product is None:
                if values["price"] is None:
                    first_result.error("priceRequired")
                    continue
                first_result.action = "create"
                for field_key in ("category", "name_en", "name_ar", "description_en", "description_ar", "price"):
                    if values[field_key] not in (None, ""):
                        first_result.change(field_key, "", values[field_key])
                first_result.change("available", "", True if values["available"] is None else values["available"])
            else:
                current_category = product.category.name_en or product.category.name_ar
                if _key(values["category"]) not in (_key(product.category.name_en), _key(product.category.name_ar)):
                    product_fields["category"] = values["category"]
                    first_result.change("category", current_category, values["category"])
                for field_key in ("name_en", "name_ar", "description_en", "description_ar"):
                    if values[field_key] and values[field_key] != getattr(product, field_key):
                        product_fields[field_key] = values[field_key]
                        first_result.change(field_key, getattr(product, field_key), values[field_key])
                if values["price"] is not None and values["price"] != product.price:
                    product_fields["price"] = values["price"]
                    first_result.change("price", product.price, values["price"])
                if values["available"] is not None and values["available"] != product.is_available:
                    product_fields["is_available"] = values["available"]
                    first_result.change("available", product.is_available, values["available"])
                if product_fields:
                    first_result.action = "update"

            variants = {}
            if product is not None:
                for variant in product.variants.all():
                    for name in (variant.name_en, variant.name_ar):
                        if name:
                            variants.setdefault(name.strip().casefold(), variant)
            size_ops = []
            for result, parsed in group["rows"]:
                size_name = parsed["size_en"] or parsed["size_ar"]
                if not size_name:
                    continue
                variant = variants.get(_key(parsed["size_en"])) if parsed["size_en"] else None
                variant = variant or (variants.get(_key(parsed["size_ar"])) if parsed["size_ar"] else None)
                if variant is None:
                    if parsed["size_price"] is None:
                        result.error("sizePriceRequired")
                        continue
                    multiplier = parsed["recipe_multiplier"] or Decimal("1")
                    result.action = "create"
                    result.change("size_price", "", parsed["size_price"])
                    result.change("recipe_multiplier", "", multiplier)
                    size_ops.append(("create", parsed["size_en"], parsed["size_ar"], parsed["size_price"], multiplier))
                    continue
                variant_fields = {}
                if parsed["size_ar"] and parsed["size_ar"] != variant.name_ar:
                    variant_fields["name_ar"] = parsed["size_ar"]
                    result.change("size_ar", variant.name_ar, parsed["size_ar"])
                if parsed["size_price"] is not None and parsed["size_price"] != variant.price:
                    variant_fields["price"] = parsed["size_price"]
                    result.change("size_price", variant.price, parsed["size_price"])
                if parsed["recipe_multiplier"] is not None and parsed["recipe_multiplier"] != variant.recipe_multiplier:
                    variant_fields["recipe_multiplier"] = parsed["recipe_multiplier"]
                    result.change("recipe_multiplier", variant.recipe_multiplier, parsed["recipe_multiplier"])
                if variant_fields:
                    if result.action != "create":
                        result.action = "update"
                    size_ops.append(("update", variant, variant_fields))
            if any(result.errors for result, _ in group["rows"]):
                continue
            if product is None or product_fields or size_ops:
                self._ops.append((product, values, product_fields, size_ops))
        return results

    def apply(self):
        created_categories = {}
        next_sort = (Category.objects.filter(branch=self.branch).order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1

        def category_for(name):
            nonlocal next_sort
            key = _key(name)
            found = Category.objects.filter(branch=self.branch, name_en__iexact=name).first() or Category.objects.filter(branch=self.branch, name_ar__iexact=name).first()
            if found:
                return found
            if key not in created_categories:
                created_categories[key] = Category.objects.create(branch=self.branch, name_en=self._new_categories.get(key, name), name_ar="", sort_order=next_sort)
                next_sort += 1
            return created_categories[key]

        for product, values, product_fields, size_ops in self._ops:
            if product is None:
                product = Product.objects.create(
                    branch=self.branch, category=category_for(values["category"]),
                    name_en=values["name_en"], name_ar=values["name_ar"],
                    description_en=values["description_en"], description_ar=values["description_ar"],
                    price=values["price"], cost_price=Decimal("0"),
                    is_available=True if values["available"] is None else values["available"],
                )
            elif product_fields:
                if "category" in product_fields:
                    product_fields["category"] = category_for(product_fields["category"])
                for key, value in product_fields.items():
                    setattr(product, key, value)
                product.save(update_fields=[*product_fields, "updated_at"])
            next_variant_sort = (product.variants.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1
            for op in size_ops:
                if op[0] == "create":
                    _, name_en, name_ar, price, multiplier = op
                    ProductVariant.objects.create(
                        product=product, name_en=name_en or name_ar, name_ar=name_ar,
                        price=price, recipe_multiplier=multiplier, sort_order=next_variant_sort,
                    )
                    next_variant_sort += 1
                else:
                    _, variant, variant_fields = op
                    for key, value in variant_fields.items():
                        setattr(variant, key, value)
                    variant.save(update_fields=[*variant_fields, "updated_at"])


# ── Recipes ──────────────────────────────────────────────────────────────────

class RecipeImporter(Importer):
    kind = "recipes"
    sheet_title = "Recipes"
    columns = [
        Column("product", "Dish", "Dish name exactly as on the menu.", required=True, width=28, example="Chicken Karahi", choices="Dishes"),
        Column("ingredient", "Ingredient", "Stock item name exactly as on the Stock page.", required=True, width=26, example="Chicken", choices="Ingredients"),
        Column("quantity", "Quantity", "How much ONE portion of the dish uses. Sizes scale this by their recipe multiplier.", required=True, width=11, example="500"),
        Column("unit", "Unit", "Unit of the quantity: kg, g, l, ml, pcs or dozen. Must fit the stock item's unit (g works for an item counted in kg).", required=True, width=10, example="g", choices="Units"),
    ]
    instructions = [
        "Recipe import", *COMMON_STEPS,
        "One row per ingredient. A dish listed here gets exactly the ingredients listed for it: an ingredient you remove from a dish's rows is removed from its recipe.",
        "Dishes not listed in this file keep their recipes as they are.",
    ]

    def _lines(self):
        return RecipeItem.objects.filter(product__branch=self.branch).select_related("product", "inventory_item").order_by("product__name_en", "id")

    def template_rows(self):
        return [{
            "product": line.product.name_en or line.product.name_ar, "ingredient": line.inventory_item.name,
            "quantity": line.quantity, "unit": line.unit,
        } for line in self._lines()]

    def lists(self):
        products = Product.objects.filter(branch=self.branch).order_by("name_en")
        items = InventoryItem.objects.filter(branch=self.branch).order_by("name")
        return {"Dishes": [p.name_en or p.name_ar for p in products], "Ingredients": [i.name for i in items], "Units": UNITS}

    def plan(self, rows):
        products = {}
        for product in Product.objects.filter(branch=self.branch).prefetch_related("recipe_items__inventory_item"):
            for name in (product.name_en, product.name_ar):
                if name:
                    products.setdefault(name.strip().casefold(), product)
        items = {item.name.strip().casefold(): item for item in InventoryItem.objects.filter(branch=self.branch)}

        groups = {}
        results = []
        for row in rows:
            values = row.values
            product_name, ingredient_name = text(values.get("product")), text(values.get("ingredient"))
            result = RowResult(row.number, f"{product_name} · {ingredient_name}")
            results.append(result)
            if not product_name:
                result.error("required", column=self.col("product").header)
            if not ingredient_name:
                result.error("required", column=self.col("ingredient").header)
            quantity, problem = decimal(values.get("quantity"), self.col("quantity"), allow_zero=False)
            if problem:
                result.errors.append(problem)
            elif quantity is None:
                result.error("required", column=self.col("quantity").header)
            unit = text(values.get("unit"))
            if not unit:
                result.error("required", column=self.col("unit").header)
            _limit(result, unit, self.col("unit"), 32)
            product = products.get(_key(product_name)) if product_name else None
            if product_name and product is None:
                result.error("dishNotFound", name=product_name)
            item = items.get(_key(ingredient_name)) if ingredient_name else None
            if ingredient_name and item is None:
                result.error("ingredientNotFound", name=ingredient_name)
            if item is not None and unit and not units_compatible(unit, item.unit):
                result.error("unitIncompatible", unit=unit, stockUnit=item.unit, ingredient=item.name)
            if product is None:
                continue
            group = groups.setdefault(product.pk, {"product": product, "lines": {}, "rows": []})
            if item is not None:
                if item.pk in group["lines"]:
                    result.error("duplicateInFile", firstRow=group["lines"][item.pk])
                else:
                    group["lines"][item.pk] = row.number
            group["rows"].append((result, item, quantity, unit))

        self._ops = []
        for group in groups.values():
            product = group["product"]
            old = {line.inventory_item_id: line for line in product.recipe_items.all()}
            new_lines = []
            for result, item, quantity, unit in group["rows"]:
                if result.errors:
                    continue
                current = old.get(item.pk)
                if current is None:
                    result.action = "create"
                    result.change("quantity", "", f"{show(quantity)} {unit}")
                elif current.quantity != quantity or current.unit.casefold() != unit.casefold():
                    result.action = "update"
                    result.change("quantity", f"{show(current.quantity)} {current.unit}", f"{show(quantity)} {unit}")
                new_lines.append(RecipeItem(product=product, inventory_item=item, quantity=quantity, unit=unit))
            for item_id, line in old.items():
                if item_id not in group["lines"]:
                    removed = RowResult(None, f"{product.name_en or product.name_ar} · {line.inventory_item.name}", action="delete")
                    removed.change("quantity", f"{show(line.quantity)} {line.unit}", "")
                    results.append(removed)
            changed = any(result.action != "unchanged" for result, *_ in group["rows"]) or set(old) - set(group["lines"])
            if changed:
                self._ops.append((product, new_lines))
        return results

    def apply(self):
        from inventory.views import _sync_product_cost_price

        for product, lines in self._ops:
            # Same replace-all as PUT /products/<id>/recipe/, and the derived
            # cost price is refreshed the same way.
            RecipeItem.objects.filter(product=product).delete()
            RecipeItem.objects.bulk_create(lines)
            # Re-read: plan() prefetched the OLD recipe onto this instance, and
            # costing it would price the lines that were just replaced.
            _sync_product_cost_price(Product.objects.get(pk=product.pk))


IMPORTERS = {importer.kind: importer for importer in (StockImporter, ProductImporter, RecipeImporter)}
