"""
Bulk import of the whole restaurant — stock, menu and recipes — from ONE
Excel workbook with a sheet for each, filled in that order.

One file instead of three is what lets a layman get it right: a recipe can
use an ingredient or a dish typed into the earlier sheets of the same file,
so there is no "import stock first, then come back" order to remember.

Every sheet works in two passes over the uploaded file:

  plan()  reads each row, validates it against the database AND the earlier
          sheets of the file, and records what WOULD happen — create /
          update (with every old -> new value) / unchanged / delete / error —
          without writing anything. This is the preview the admin reviews.
  apply() performs exactly that plan. The view runs every sheet's apply()
          inside one transaction and only when no sheet found an error, so
          an import either lands completely or not at all.

Matching is by name (case-insensitive), the same rule the manual forms use to
reject duplicates. A name that is NOT found but is close to an existing one
("Chiken") is never silently created as a second item: a recipe gets an
error naming the likely match, and a new stock item or dish gets a visible
note. Rows left out of the file are never deleted — except a recipe, which is
replaced as a whole for each dish the file lists, and each removed
ingredient line is shown in the preview as a delete.
"""

import difflib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from openpyxl.utils import get_column_letter, quote_sheetname

from catalog.models import Category, Product, ProductVariant
from inventory.models import InventoryItem, RecipeItem, StockMovementReason
from inventory.services import adjust_stock, units_compatible
from purchasing.models import Supplier

from .models import ImportDownload
from .workbook import MAX_ROWS, Column, SheetSpec, decimal, paragraph, sheet_column_range, show, text, yes_no

UNITS = ["kg", "g", "l", "ml", "pcs", "dozen"]
STOCK_SHEET, MENU_SHEET, RECIPE_SHEET = "1 Stock", "2 Menu", "3 Recipes"
ACTIONS = ("create", "update", "unchanged", "delete", "error")
DOWNLOAD_DAYS = 90


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

    def error(self, code, field=None, **params):
        # `field` is the column the problem is in, so the marked-up file can
        # colour that exact cell; None means the row as a whole.
        self.errors.append({"code": code, "field": field, "params": params})

    def note(self, code, **params):
        self.notes.append({"code": code, "params": params})

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
        result.error("tooLong", column.key, column=column.header, max=max_length)


def _similar(name, known):
    """The existing name `name` is most likely a typo of, or None.
    `known` maps casefolded key -> name as the admin wrote it."""
    match = difflib.get_close_matches(_key(name), list(known), n=1, cutoff=0.8)
    return known[match[0]] if match else None


class Batch:
    """What the sheets of one upload learn about each other while planning
    (a stock item the Stock sheet adds, a dish the Menu sheet adds) and the
    rows they create while applying."""

    def __init__(self, branch):
        self.branch = branch
        # key -> (name, unit) for every stock item the Stock sheet adds.
        self.new_items = {}
        # item pk -> unit, for existing items whose unit the Stock sheet changes.
        self.new_units = {}
        # [(result, item, unit)] unit changes, checked against recipes last.
        self.unit_changes = []
        # key -> name for every dish the Menu sheet adds.
        self.new_products = {}
        # key -> (name, unit) for ingredients only a recipe row mentions.
        self.auto_items = {}
        # pks of existing dishes whose whole recipe the file sets.
        self.recipe_products = set()
        # key -> current stock the Stock sheet gives a new item (None = empty).
        self.new_item_stock = {}
        # key of a new item -> names of the dishes whose recipes use it.
        self.item_dishes = {}
        self.created_items = {}
        self.created_products = {}
        # key -> {field: Decimal} for each stock item as it was when the file
        # was downloaded (ImportDownload); None for a file without a code.
        self.at_download = None


class SheetImporter:
    key = ""
    sheet_title = ""
    # The column a row-level problem is marked on in the error file.
    name_field = ""
    columns: list[Column] = []
    rules: list[str] = []

    def __init__(self, batch):
        self.batch = batch
        self.branch = batch.branch

    def col(self, key):
        return next(column for column in self.columns if column.key == key)

    def spec(self):
        return SheetSpec(self.sheet_title, self.columns, self.template_rows(), self.rules)

    def template_rows(self):
        raise NotImplementedError

    def plan(self, rows):
        raise NotImplementedError

    def apply(self):
        raise NotImplementedError


# ── Stock ────────────────────────────────────────────────────────────────────

class StockImporter(SheetImporter):
    key = "stock"
    sheet_title = STOCK_SHEET
    name_field = "name"
    columns = [
        Column("name", "Name", "The ingredient or item you keep in stock. A name that already exists updates that item; a new name adds it.", required=True, width=28, example="Chicken"),
        Column("unit", "Unit", "How you count it. Pick from the list: kg, g, l, ml, pcs or dozen.", required=True, width=10, example="kg", choices="Units", strict=True),
        Column("current_stock", "Current stock", "How much you have right now, in the unit above. Leave empty to keep the current amount. A change is saved as a stock adjustment.", example="25", number="zero"),
        Column("par_level", "Par level", "Warn me when stock falls to this amount. Leave empty to keep the current level.", example="5", number="zero"),
        Column("cost_per_unit", "Cost per unit", "What you pay for ONE unit (one kg, one piece...). Number only, no Rs. Leave empty to keep the current cost.", width=16, example="650", number="zero"),
        Column("supplier", "Supplier", "Optional. Pick from the list, or type a new supplier name to add it.", width=26, example="Al Noor Poultry", choices="Suppliers"),
    ]
    rules = [
        "One row per stock item. Recipes (sheet 3) can only use items listed here.",
        "Removing a row from this sheet does NOT delete the item from the system.",
    ]

    # Values the restaurant changes by itself between a download and an
    # upload: sales and adjustments move stock, a received purchase sets the
    # cost. A stale file must not quietly put the old number back.
    tracked = ("current_stock", "par_level", "cost_per_unit")

    def _items(self):
        return InventoryItem.objects.filter(branch=self.branch).select_related("supplier").order_by("name")

    def downloaded(self):
        """{key: {field: value}} as the file is filled now, for ImportDownload."""
        return {_key(item.name): {key: format(getattr(item, key), "f") for key in self.tracked} for item in self._items()}

    def _against_download(self, result, item, numbers):
        """Drop the tracked values the admin left as downloaded, and refuse
        those the admin changed while the restaurant changed them too.

        Left as downloaded: the file still shows what the item had then, so
        it says nothing about what it should have now — today's value is
        kept, with a note when it moved. Both changed: neither number can be
        trusted to be the one meant, so the admin decides on a fresh file."""
        at_download = self.batch.at_download.get(_key(item.name)) if self.batch.at_download is not None else None
        if at_download is None:
            return
        for key in self.tracked:
            value, then, now = numbers[key], at_download.get(key), getattr(item, key)
            if value is None or then is None:
                continue
            column = self.col(key).header
            if value == then:
                numbers[key] = None
                if now != then:
                    result.note("keptCurrent", column=column, downloaded=show(then), now=show(now))
            elif now != then:
                result.error("changedSinceDownload", key, column=column, downloaded=show(then), now=show(now), file=show(value))

    def template_rows(self):
        return [{
            "name": item.name, "unit": item.unit, "current_stock": item.current_stock,
            "par_level": item.par_level, "cost_per_unit": item.cost_per_unit,
            "supplier": item.supplier.name_en if item.supplier else "",
        } for item in self._items()]

    def plan(self, rows):
        existing = {item.name.strip().casefold(): item for item in self._items()}
        known = {key: item.name for key, item in existing.items()}
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
                result.error("required", "name", column=self.col("name").header)
            _limit(result, name, self.col("name"), 255)
            if name and _key(name) in seen:
                result.error("duplicateInFile", "name", firstRow=seen[_key(name)])
            elif name:
                seen[_key(name)] = row.number
            unit = text(values.get("unit"))
            if not unit:
                result.error("required", "unit", column=self.col("unit").header)
            _limit(result, unit, self.col("unit"), 32)
            numbers = {}
            for key in ("current_stock", "par_level", "cost_per_unit"):
                numbers[key], problem = decimal(values.get(key), self.col(key))
                if problem:
                    result.errors.append(problem)
            supplier_name = text(values.get("supplier"))
            _limit(result, supplier_name, self.col("supplier"), 255)
            item = existing.get(_key(name)) if name else None
            if item is not None:
                self._against_download(result, item, numbers)
            if name and unit and item is None:
                # Known to the Recipes sheet even if this row has an error, so
                # a recipe using it is not wrongly reported as unknown too.
                self.batch.new_items.setdefault(_key(name), (name, unit))
                self.batch.new_item_stock.setdefault(_key(name), numbers["current_stock"])
            if result.errors:
                continue

            supplier = None
            if supplier_name:
                supplier = suppliers.get(_key(supplier_name))
                if supplier is None:
                    self._new_suppliers.setdefault(_key(supplier_name), supplier_name)
                    result.note("newSupplier", name=supplier_name)
            stock, par, cost = numbers["current_stock"], numbers["par_level"], numbers["cost_per_unit"]
            if item is None:
                similar = _similar(name, known)
                if similar:
                    result.note("similarExists", name=name, existing=similar)
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
            if unit != text(item.unit):
                fields["unit"] = unit
                result.change("unit", item.unit, unit)
                self.batch.new_units[item.pk] = unit
                self.batch.unit_changes.append((result, item, unit))
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

    def check_unit_changes(self):
        """A unit change must not break a recipe the file leaves as it is.
        Recipes the file sets were already checked against the new unit."""
        for result, item, unit in self.batch.unit_changes:
            lines = RecipeItem.objects.filter(inventory_item=item).exclude(product_id__in=self.batch.recipe_products).select_related("product")
            broken = sorted({line.product.name_en or line.product.name_ar for line in lines if not units_compatible(line.unit, unit)})
            if broken:
                result.error("unitBreaksRecipes", "unit", unit=unit, products=", ".join(broken))

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
                self.batch.created_items[_key(name)] = item
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


class ProductImporter(SheetImporter):
    key = "products"
    sheet_title = MENU_SHEET
    name_field = "name_en"
    columns = [
        Column("category", "Category", "Menu section, e.g. Burgers. Pick from the list, or type a new name to add a new category.", required=True, width=20, example="Karahi", choices="Categories"),
        Column("name_en", "Name (English)", "Dish name in English. English or Arabic name is required.", width=28, example="Chicken Karahi"),
        Column("name_ar", "Name (Arabic)", "Dish name in Arabic (optional if the English name is filled).", width=24, example="كراهي دجاج"),
        Column("description_en", "Description (English)", "Optional.", width=32),
        Column("description_ar", "Description (Arabic)", "Optional.", width=32),
        Column("price", "Price", "Menu price, number only. Required for a new dish. For a dish with sizes, the price of the regular size.", width=10, example="1400", number="zero"),
        Column("available", "Available", "Yes or No. Leave empty to keep it as it is (new dishes are available).", width=11, example="Yes", choices="YesNo", strict=True),
        Column("size_en", "Size (English)", "Only for dishes sold in sizes: one row per size, repeating the dish name. e.g. Half KG / 1 KG.", width=16, example="1 KG"),
        Column("size_ar", "Size (Arabic)", "Optional Arabic size name.", width=14),
        Column("size_price", "Size price", "Price of this size, number only. Required for a new size.", width=11, example="2600", number="zero"),
        Column("recipe_multiplier", "Recipe multiplier", "How many times the dish's recipe this size uses: Half KG = 1, 1 KG = 2. Empty = 1 for a new size.", width=17, example="2", number="positive"),
    ]
    rules = [
        "One row per dish. A dish with sizes takes one row per size: repeat the dish's name, category and price on each of its rows.",
        "Recipes (sheet 3) can only use dishes listed here.",
        "Removing a row from this sheet does NOT delete the dish or size from the system.",
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

    def _read(self, row, result):
        values = row.values
        parsed = {key: text(values.get(key)) for key in ("category", "name_en", "name_ar", "size_en", "size_ar")}
        parsed.update({key: paragraph(values.get(key)) for key in ("description_en", "description_ar")})
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
        known_products = {key: product.name_en or product.name_ar for key, product in by_name.items()}
        categories = {}
        for category in Category.objects.filter(branch=self.branch):
            for name in (category.name_en, category.name_ar):
                if name:
                    categories.setdefault(name.strip().casefold(), category)
        known_categories = {key: category.name_en or category.name_ar for key, category in categories.items()}

        groups = {}
        results = []
        for row in rows:
            parsed_preview = text(row.values.get("name_en")) or text(row.values.get("name_ar"))
            size_preview = text(row.values.get("size_en")) or text(row.values.get("size_ar"))
            result = RowResult(row.number, f"{parsed_preview} · {size_preview}" if size_preview else parsed_preview)
            results.append(result)
            parsed = self._read(row, result)
            if not parsed["name_en"] and not parsed["name_ar"]:
                result.error("nameRequired", "name_en")
            if not parsed["category"]:
                result.error("required", "category", column=self.col("category").header)
            has_size_values = parsed["size_price"] is not None or parsed["recipe_multiplier"] is not None
            if has_size_values and not (parsed["size_en"] or parsed["size_ar"]):
                result.error("sizeNameRequired", "size_en")
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
                        result.error("conflictingValue", field_key, column=self.col(field_key).header, firstRow=group["first"])
                    elif mine not in (None, "") and theirs in (None, ""):
                        group["values"][field_key] = mine
            size_name = parsed["size_en"] or parsed["size_ar"]
            if size_name:
                if _key(size_name) in group["sizes"]:
                    result.error("duplicateInFile", "size_en", firstRow=group["sizes"][_key(size_name)])
                else:
                    group["sizes"][_key(size_name)] = row.number
            elif any(not entry[1]["size_en"] and not entry[1]["size_ar"] for entry in group["rows"]):
                result.error("duplicateInFile", "name_en", firstRow=group["first"])
            group["rows"].append((result, parsed))

        self._ops = []
        self._new_categories = {}
        for group in groups.values():
            values = group["values"]
            first_result = group["rows"][0][0]
            product = by_name.get(_key(values["name_en"])) if values["name_en"] else None
            product = product or (by_name.get(_key(values["name_ar"])) if values["name_ar"] else None)
            if product is None:
                # Known to the Recipes sheet even if a row here has an error.
                for name in (values["name_en"], values["name_ar"]):
                    if name:
                        self.batch.new_products.setdefault(_key(name), name)
            if any(result.errors for result, _ in group["rows"]):
                continue

            if values["category"] and _key(values["category"]) not in categories:
                self._new_categories.setdefault(_key(values["category"]), values["category"])
                similar = _similar(values["category"], known_categories)
                if similar:
                    first_result.note("similarExists", name=values["category"], existing=similar)
                first_result.note("newCategory", name=values["category"])

            product_fields = {}
            if product is None:
                if values["price"] is None:
                    first_result.error("priceRequired", "price")
                    continue
                similar = _similar(values["name_en"] or values["name_ar"], known_products)
                if similar:
                    first_result.note("similarExists", name=values["name_en"] or values["name_ar"], existing=similar)
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
                    # Compared the way the file was read, so stray spaces
                    # already in the database are not reported as a change.
                    read = paragraph if field_key.startswith("description") else text
                    if values[field_key] and values[field_key] != read(getattr(product, field_key)):
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
                        result.error("sizePriceRequired", "size_price")
                        continue
                    multiplier = parsed["recipe_multiplier"] or Decimal("1")
                    result.action = "create"
                    result.change("size_price", "", parsed["size_price"])
                    result.change("recipe_multiplier", "", multiplier)
                    size_ops.append(("create", parsed["size_en"], parsed["size_ar"], parsed["size_price"], multiplier))
                    continue
                variant_fields = {}
                if parsed["size_ar"] and parsed["size_ar"] != text(variant.name_ar):
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
                for name in (values["name_en"], values["name_ar"]):
                    if name:
                        self.batch.created_products[_key(name)] = product
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

class RecipeImporter(SheetImporter):
    key = "recipes"
    sheet_title = RECIPE_SHEET
    name_field = "product"
    columns = [
        Column("product", "Dish", "Pick the dish from the list. The list is the dishes on sheet 2 Menu, including new ones you add there.", required=True, width=28, example="Chicken Karahi", choices="Dishes", strict=True),
        Column("ingredient", "Ingredient", "Pick the stock item from the list. The list is the items on sheet 1 Stock, including new ones you add there.", required=True, width=26, example="Chicken", choices="Ingredients", strict=True),
        Column("quantity", "Quantity", "How much ONE portion of the dish uses, number only. Sizes scale this by their recipe multiplier.", required=True, width=11, example="500", number="positive"),
        Column("unit", "Unit", "Unit of the quantity: kg, g, l, ml, pcs or dozen. Must fit the stock item's unit (g works for an item counted in kg).", required=True, width=10, example="g", choices="Units", strict=True),
    ]
    rules = [
        "One row per ingredient of a dish. A dish listed here gets exactly the ingredients listed for it: an ingredient you remove from a dish's rows is removed from its recipe.",
        "Dishes not listed on this sheet keep their recipes as they are.",
    ]

    def _lines(self):
        return RecipeItem.objects.filter(product__branch=self.branch).select_related("product", "inventory_item").order_by("product__name_en", "id")

    def template_rows(self):
        return [{
            "product": line.product.name_en or line.product.name_ar, "ingredient": line.inventory_item.name,
            "quantity": line.quantity, "unit": line.unit,
        } for line in self._lines()]

    def _ingredient(self, name, unit, result, items):
        """(reference, stock unit) for an ingredient name, or (None, None) with
        the reason on `result`. A reference is an InventoryItem, or the key of
        an item this file adds."""
        key = _key(name)
        item = items.get(key)
        if item is not None:
            return item, self.batch.new_units.get(item.pk, item.unit)
        if key in self.batch.new_items:
            return key, self.batch.new_items[key][1]
        if key in self.batch.auto_items:
            return key, self.batch.auto_items[key][1]
        known = {k: i.name for k, i in items.items()}
        known.update({k: value[0] for k, value in (*self.batch.new_items.items(), *self.batch.auto_items.items())})
        similar = _similar(name, known)
        if similar:
            # Never add "Chiken" next to "Chicken": that would split stock in two.
            result.error("ingredientSimilar", "ingredient", name=name, existing=similar)
            return None, None
        if not unit:
            return None, None
        self.batch.auto_items[key] = (name, unit)
        result.note("newIngredient", name=name, unit=unit)
        return key, unit

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
                result.error("required", "product", column=self.col("product").header)
            if not ingredient_name:
                result.error("required", "ingredient", column=self.col("ingredient").header)
            quantity, problem = decimal(values.get("quantity"), self.col("quantity"), allow_zero=False)
            if problem:
                result.errors.append(problem)
            elif quantity is None:
                result.error("required", "quantity", column=self.col("quantity").header)
            unit = text(values.get("unit"))
            if not unit:
                result.error("required", "unit", column=self.col("unit").header)
            _limit(result, unit, self.col("unit"), 32)

            product = None
            if product_name:
                product = products.get(_key(product_name))
                if product is None and _key(product_name) in self.batch.new_products:
                    product = _key(product_name)
                if product is None:
                    known = {key: p.name_en or p.name_ar for key, p in products.items()}
                    known.update(self.batch.new_products)
                    similar = _similar(product_name, known)
                    if similar:
                        result.error("dishSimilar", "product", name=product_name, existing=similar)
                    else:
                        result.error("dishNotFound", "product", name=product_name)

            item, stock_unit = (None, None)
            if ingredient_name:
                item, stock_unit = self._ingredient(ingredient_name, unit, result, items)
            if item is not None and unit and not units_compatible(unit, stock_unit):
                result.error("unitIncompatible", "unit", unit=unit, stockUnit=stock_unit, ingredient=ingredient_name)
            if product is None:
                continue
            if isinstance(item, str):
                dish = (product.name_en or product.name_ar) if isinstance(product, Product) else self.batch.new_products[product]
                self.batch.item_dishes.setdefault(item, set()).add(dish)
            group_key = product.pk if isinstance(product, Product) else product
            group = groups.setdefault(group_key, {"product": product, "lines": {}, "rows": []})
            if item is not None:
                item_key = item.pk if isinstance(item, InventoryItem) else item
                if item_key in group["lines"]:
                    result.error("duplicateInFile", "ingredient", firstRow=group["lines"][item_key])
                else:
                    group["lines"][item_key] = row.number
            group["rows"].append((result, item, quantity, unit))

        self._ops = []
        for group in groups.values():
            product = group["product"]
            existing = isinstance(product, Product)
            if existing:
                self.batch.recipe_products.add(product.pk)
            old = {line.inventory_item_id: line for line in product.recipe_items.all()} if existing else {}
            new_lines = []
            for result, item, quantity, unit in group["rows"]:
                if result.errors:
                    continue
                current = old.get(item.pk) if isinstance(item, InventoryItem) else None
                if current is None:
                    result.action = "create"
                    result.change("quantity", "", f"{show(quantity)} {unit}")
                elif current.quantity != quantity or current.unit.casefold() != unit.casefold():
                    result.action = "update"
                    result.change("quantity", f"{show(current.quantity)} {current.unit}", f"{show(quantity)} {unit}")
                new_lines.append((item, quantity, unit))
            if existing:
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

        for key, (name, unit) in self.batch.auto_items.items():
            self.batch.created_items[key] = InventoryItem.objects.create(
                branch=self.branch, name=name, unit=unit, current_stock=0, par_level=0, cost_per_unit=0,
            )
        for product, lines in self._ops:
            if not isinstance(product, Product):
                product = self.batch.created_products[product]
            # Same replace-all as PUT /products/<id>/recipe/, and the derived
            # cost price is refreshed the same way.
            RecipeItem.objects.filter(product=product).delete()
            RecipeItem.objects.bulk_create([
                RecipeItem(
                    product=product, quantity=quantity, unit=unit,
                    inventory_item=item if isinstance(item, InventoryItem) else self.batch.created_items[item],
                ) for item, quantity, unit in lines
            ])
            # Re-read: plan() prefetched the OLD recipe onto this instance, and
            # costing it would price the lines that were just replaced.
            _sync_product_cost_price(Product.objects.get(pk=product.pk))


SHEETS = (StockImporter, ProductImporter, RecipeImporter)

START_LINES = [
    "How to fill this file",
    "",
    "1. Fill the sheets in order: 1 Stock, then 2 Menu, then 3 Recipes.",
    "   Each sheet already contains everything you have now. Change rows, or add new rows under them.",
    "   A recipe picks its dish from sheet 2 and its ingredients from sheet 1, so add new items there first.",
    "2. Orange headings must be filled; grey ones are optional. Click any cell to see what to write in it.",
    "   Use the dropdown lists where a column has one. Write numbers only (1400, not Rs. 1400).",
    "3. Do not rename the sheets or change the heading rows.",
    "4. Save the file and upload it on the admin page. You will see every change before anything is saved.",
    "   If something is wrong you can download this file back with the wrong cells marked in red.",
]


class RestaurantImport:
    """The whole workbook: every sheet planned in order against one Batch."""

    def __init__(self, branch):
        self.branch = branch
        self.batch = Batch(branch)
        self.sheets = [sheet_class(self.batch) for sheet_class in SHEETS]

    def stock_sheet(self):
        return self.sheets[0]

    def lists(self):
        units = list(UNITS)
        for unit in InventoryItem.objects.filter(branch=self.branch).values_list("unit", flat=True).distinct():
            if unit and _key(unit) not in (_key(u) for u in units):
                units.append(unit)
        letter = {column.key: get_column_letter(index) for index, column in enumerate(ProductImporter.columns, start=1)}
        menu = quote_sheetname(MENU_SHEET)
        # A dish is listed by its English name, or its Arabic one when it has
        # no English name — the names the Recipes sheet matches. Formulas, so
        # a dish typed into the Menu sheet shows up in the dropdown at once.
        dishes = [
            f'=IF({menu}!{letter["name_en"]}{row}<>"",{menu}!{letter["name_en"]}{row},{menu}!{letter["name_ar"]}{row})'
            for row in range(2, MAX_ROWS + 2)
        ]
        return {
            "Units": units,
            "YesNo": ["Yes", "No"],
            "Suppliers": sorted(s.name_en for s in Supplier.objects.all() if s.name_en),
            "Categories": [c.name_en or c.name_ar for c in Category.objects.filter(branch=self.branch).order_by("sort_order", "id")],
            "Dishes": dishes,
            "Ingredients": sheet_column_range(STOCK_SHEET, 1),
        }

    def specs(self):
        return [sheet.spec() for sheet in self.sheets]

    def draft_specs(self, draft):
        """The current data with an AI draft (ai.draft()) merged in, as
        SheetSpecs, and the cells it estimated as [(sheet key, row number,
        column key)] — the workbook the admin would have made by hand."""
        stock, menu, recipes = self.sheets
        guessed = {}  # id(row dict) -> estimated column keys

        stock_rows = stock.template_rows()
        by_name = {_key(row["name"]): row for row in stock_rows}
        for values, estimated in draft["stock"]:
            row = by_name.get(_key(values.get("name")))
            if row is None:
                row = by_name[_key(values.get("name"))] = {}
                stock_rows.append(row)
            row.update({key: value for key, value in values.items() if value not in (None, "")})
            guessed.setdefault(id(row), set()).update(estimated)

        menu_rows = menu.template_rows()
        dishes = {}
        for row in menu_rows:
            dishes.setdefault(_key(row.get("name_en") or row.get("name_ar")), []).append(row)
        for values, estimated in draft["products"]:
            dish_rows = dishes.setdefault(_key(values.get("name_en") or values.get("name_ar")), [])
            dish_values = {key: values[key] for key in PRODUCT_FIELDS if values.get(key) not in (None, "")}
            size_values = {key: value for key, value in values.items() if key not in PRODUCT_FIELDS and value not in (None, "")}
            # A dish's own values sit on every one of its size rows, or the
            # rows would disagree with each other (conflictingValue).
            for row in dish_rows:
                row.update(dish_values)
            size = _key(values.get("size_en") or values.get("size_ar"))
            target = next((row for row in dish_rows if _key(row.get("size_en") or row.get("size_ar")) == size), None)
            if target is None:
                target = {key: dish_rows[0].get(key) for key in PRODUCT_FIELDS} if dish_rows else {}
                target.update(dish_values)
                dish_rows.append(target)
                menu_rows.append(target)
            target.update(size_values)
            guessed.setdefault(id(target), set()).update(estimated)

        drafted = {}
        for values, estimated in draft["recipes"]:
            drafted.setdefault(_key(values.get("product")), []).append((values, estimated))
        # A dish the draft gives a recipe gets exactly that recipe, the same
        # replace-all rule as the Recipes sheet itself.
        recipe_rows = [row for row in recipes.template_rows() if _key(row["product"]) not in drafted]
        for lines in drafted.values():
            for values, estimated in lines:
                row = dict(values)
                recipe_rows.append(row)
                guessed[id(row)] = set(estimated)

        rows = {stock.key: stock_rows, menu.key: menu_rows, recipes.key: recipe_rows}
        specs, marks = [], []
        for sheet in self.sheets:
            specs.append(SheetSpec(sheet.sheet_title, sheet.columns, rows[sheet.key], sheet.rules))
            for number, row in enumerate(rows[sheet.key], start=2):
                for key in sorted(guessed.get(id(row), ())):
                    marks.append((sheet.key, number, key))
        return specs, marks

    def record_download(self):
        """The file code for a workbook filled with the data as it is now.
        Codes older than DOWNLOAD_DAYS are dropped: a file that old is stale
        everywhere, and its upload is asked to start from a fresh one."""
        ImportDownload.objects.filter(branch=self.branch, created_at__lt=timezone.now() - timedelta(days=DOWNLOAD_DAYS)).delete()
        return ImportDownload.objects.create(branch=self.branch, stock=self.stock_sheet().downloaded()).pk

    def find_download(self, code):
        """The stock values recorded for a file code, or None when this
        branch has no such download (mistyped, another branch's, expired)."""
        try:
            download = ImportDownload.objects.filter(branch=self.branch, pk=code).first()
        except ValidationError:  # not a UUID at all
            return None
        if download is None:
            return None
        return {key: {field: Decimal(value) for field, value in values.items()} for key, values in download.stock.items()}

    def plan(self, parsed, at_download=None):
        """{sheet key: [RowResult]} for {sheet key: ParsedSheet}, checked
        against find_download()'s values when the file has a code."""
        self.batch.at_download = at_download
        results = {sheet.key: sheet.plan(parsed[sheet.key].rows) for sheet in self.sheets}
        self.stock_sheet().check_unit_changes()
        return results

    def apply(self):
        for sheet in self.sheets:
            sheet.apply()

    def to_buy(self):
        """Stock items this file adds with nothing in hand, and the dishes
        whose recipes use them: what to purchase before those dishes can be
        made. After apply() each carries its new id, for the purchase form."""
        items = []
        for key, (name, unit) in {**self.batch.auto_items, **self.batch.new_items}.items():
            if self.batch.new_item_stock.get(key):
                continue
            created = self.batch.created_items.get(key)
            items.append({
                "name": name, "unit": unit, "dishes": sorted(self.batch.item_dishes.get(key, ()), key=str.casefold),
                "id": str(created.pk) if created else None,
            })
        return sorted(items, key=lambda entry: entry["name"].casefold())

    def checklist(self):
        products = Product.objects.filter(branch=self.branch)
        return {
            "stock_items": InventoryItem.objects.filter(branch=self.branch).count(),
            "dishes": products.count(),
            "dishes_without_recipe": products.filter(recipe_items__isnull=True).count(),
        }

    def guide(self):
        """Everything the admin page shows to explain the file, built from
        the same Column definitions the upload is validated against."""
        return {
            "sheets": [{
                "key": sheet.key, "title": sheet.sheet_title, "rules": sheet.rules,
                "columns": [{
                    "key": column.key, "header": column.header, "help": column.help,
                    "example": column.example, "required": column.required,
                } for column in sheet.columns],
            } for sheet in self.sheets],
            "checklist": self.checklist(),
            "prompt": self.ai_prompt(),
        }

    def ai_prompt(self):
        """Text the admin pastes into any AI chat, with their own list or a
        menu photo, to get a file in exactly this workbook's format."""
        lines = [
            f'Make an Excel (.xlsx) file with these sheets, named exactly: "{STOCK_SHEET}", "{MENU_SHEET}", "{RECIPE_SHEET}".',
            "Write numbers as plain numbers without currency or units (1400, not Rs. 1400). Leave a cell empty when you do not know the value; do not invent prices or quantities.",
        ]
        for sheet in self.sheets:
            lines += [
                "",
                f'Sheet "{sheet.sheet_title}". Row 1 must contain exactly these column headers, in this order:',
                " | ".join(column.label for column in sheet.columns),
                "Rules for each column (* = must not be empty):",
            ]
            for column in sheet.columns:
                example = f' Example: "{column.example}".' if column.example else ""
                lines.append(f"- {column.label}: {column.help}{example}")
            lines += sheet.rules
        lists = self.lists()
        for name in ("Categories", "Suppliers"):
            if lists[name]:
                lines += ["", f"Existing {name.lower()} (use exactly these spellings when you mean one of them):", ", ".join(lists[name][:300])]
        existing = {
            "dishes": [p.name_en or p.name_ar for p in Product.objects.filter(branch=self.branch).order_by("name_en")],
            "stock items": [i.name for i in InventoryItem.objects.filter(branch=self.branch).order_by("name")],
        }
        for name, values in existing.items():
            if values:
                lines += ["", f"Existing {name} (use exactly these spellings when you mean one of them):", ", ".join(values[:300])]
        lines += ["", "Give me the finished .xlsx file to download. My data is below:", ""]
        return "\n".join(lines)
