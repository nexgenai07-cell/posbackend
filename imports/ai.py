"""
"Fill with AI": turn the admin's own words (a pasted menu, "add a chicken
pulao at 900 with its recipe") into rows for the restaurant workbook.

The AI only drafts. Its rows are merged into the current data and go through
exactly the same plan() as an uploaded Excel file, so every check, the
preview and the Confirm step are unchanged — nothing it says is saved on its
own word. Two rules are enforced here rather than trusted to the prompt:

  * Money and stock amounts are never guessed. A price, cost or stock amount
    is kept only when that number appears in the admin's own text; any other
    is dropped and the cell left for the admin.
  * Everything else it guessed (recipe quantities, a category it picked) is
    returned as `estimated` so the preview and the file mark it for checking.

Talks to any OpenAI-compatible chat endpoint (NVIDIA by default), configured
by AI_API_KEY / AI_BASE_URL / AI_MODEL.
"""

import json
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from catalog.models import Product
from inventory.models import InventoryItem, RecipeItem

# Values the AI may only copy from the admin's message, never estimate.
NEVER_GUESS = {"price", "size_price", "cost_per_unit", "current_stock", "par_level"}
MAX_TEXT = 20000

SYSTEM_PROMPT = """You prepare a draft of a restaurant's stock, menu and recipes for its admin to review.

The draft is not saved directly. It is checked exactly like an Excel file the admin uploads, and the admin sees every change, with your estimates highlighted, before confirming.

Two kinds of values:
- Money and stock amounts — price, size price, cost per unit, current stock, par level — come only from the admin's message. If the message does not state one, set it to null. Never estimate these.
- Everything else you may decide with your own kitchen knowledge, and when the admin asks for recipes you are expected to: write realistic quantities for a restaurant portion rather than leaving them out. Pick the unit a restaurant would stock the item in (rice, flour, meat and vegetables in kg; oil and milk in l; eggs and buns in pcs), and a sensible category when none is given. Every value you decide yourself goes in "estimated_fields" so the admin checks it; values the admin gave you do not.

How to fill the draft:
- Return only rows to add or change. Anything you leave out stays exactly as it is.
- When you mean something that already exists (a stock item, a dish, a category, a supplier), write its name exactly as it appears in the current data.
- A recipe is the complete list of ingredients for ONE portion of the dish's regular size, one row per ingredient. Larger sizes use the recipe multiplier instead of separate recipe rows. Leave out dishes whose existing recipe the admin did not ask to change.
- Every recipe ingredient must be an existing stock item or an item you add to "stock" (name and unit; amounts null).
- A recipe unit must fit its stock item's unit: g or kg for mass, ml or l for volume, pcs or dozen for counted items. Small amounts read best in g or ml.
- "summary": one to three short sentences, in the language the admin wrote in, saying what you drafted and anything you could not do.

What each column means:
"""


class AiDraftError(Exception):
    """A draft that could not be made. `code` is the errors.json key."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def enabled():
    return bool(settings.AI_API_KEY)


def _column_schema(column, units):
    if column.choices == "Units":
        return {"type": "string", "enum": units} if column.required else {"type": ["string", "null"], "enum": [*units, None]}
    if column.choices == "YesNo":
        return {"type": ["boolean", "null"]}
    if column.number:
        return {"type": "number"} if column.required else {"type": ["number", "null"]}
    return {"type": "string"} if column.required else {"type": ["string", "null"]}


def _row_schema(sheet, units):
    keys = [column.key for column in sheet.columns]
    properties = {column.key: _column_schema(column, units) for column in sheet.columns}
    properties["estimated_fields"] = {"type": "array", "items": {"type": "string", "enum": keys}}
    return {"type": "object", "properties": properties, "required": [*keys, "estimated_fields"], "additionalProperties": False}


def schema(restaurant):
    units = restaurant.lists()["Units"]
    sheets = {sheet.key: sheet for sheet in restaurant.sheets}
    properties = {"summary": {"type": "string"}}
    for key, sheet in sheets.items():
        properties[key] = {"type": "array", "items": _row_schema(sheet, units)}
    return {"type": "object", "properties": properties, "required": ["summary", *sheets], "additionalProperties": False}


def _current_data(branch):
    """What the restaurant has now, compact: the names the AI must reuse and
    which dishes already have a recipe."""
    recipes = {}
    for line in RecipeItem.objects.filter(product__branch=branch).select_related("product", "inventory_item").order_by("product_id", "id"):
        dish = line.product.name_en or line.product.name_ar
        recipes.setdefault(dish, []).append(f"{line.inventory_item.name} {line.quantity.normalize():f} {line.unit}")
    dishes = []
    for product in Product.objects.filter(branch=branch).select_related("category").prefetch_related("variants").order_by("name_en"):
        name = product.name_en or product.name_ar
        dishes.append({
            "name": name, "category": product.category.name_en or product.category.name_ar,
            "price": f"{product.price.normalize():f}",
            "sizes": [variant.name_en or variant.name_ar for variant in product.variants.all()],
            "recipe": recipes.get(name, []),
        })
    return {
        "stock_items": [{"name": item.name, "unit": item.unit} for item in InventoryItem.objects.filter(branch=branch).order_by("name")],
        "dishes": dishes,
    }


def _instructions(restaurant):
    lines = [SYSTEM_PROMPT]
    for sheet in restaurant.sheets:
        lines.append(f'Rows in "{sheet.key}" (sheet {sheet.sheet_title}):')
        lines += [f"- {column.key}: {column.help}" for column in sheet.columns]
        lines += [f"  {rule}" for rule in sheet.rules]
        lines.append("")
    return "\n".join(lines)


def _numbers_in(text):
    """Every number the admin wrote ("Rs. 1,400" -> 1400)."""
    found = set()
    for match in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            found.add(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def _clean(rows, sheet, given):
    """(rows, estimated keys per row). A money or stock amount is kept only
    when that number is in the admin's own text (`given`) — checked here, not
    left to the AI's word, because those are the values the AI may never
    decide."""
    keys = {column.key for column in sheet.columns}
    cleaned, estimated = [], []
    for row in rows:
        guessed = [key for key in row.get("estimated_fields", []) if key in keys]
        values = {key: row.get(key) for key in keys}
        for key in NEVER_GUESS & keys:
            value = values.get(key)
            if value is not None and (not isinstance(value, (int, float)) or Decimal(str(value)) not in given):
                values[key] = None
        if isinstance(values.get("available"), bool):
            values["available"] = "Yes" if values["available"] else "No"
        cleaned.append(values)
        estimated.append([key for key in guessed if key not in NEVER_GUESS and values.get(key) not in (None, "")])
    return cleaned, estimated


def draft(restaurant, text):
    """{"summary": str, sheet key: [(row dict, [estimated keys])]} and the
    token usage, or AiDraftError."""
    client = OpenAI(base_url=settings.AI_BASE_URL, api_key=settings.AI_API_KEY, timeout=300, max_retries=1)
    data = json.dumps(_current_data(restaurant.branch), ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=settings.AI_MODEL,
            max_tokens=16000,
            temperature=0.2,
            response_format={"type": "json_schema", "json_schema": {"name": "restaurant_draft", "schema": schema(restaurant), "strict": True}},
            messages=[
                {"role": "system", "content": _instructions(restaurant)},
                {"role": "user", "content": f"Current restaurant data (JSON):\n{data}\n\nThe admin's request:\n{text}"},
            ],
        )
    except APITimeoutError as error:
        raise AiDraftError("imports.aiTimeout") from error
    except (APIConnectionError, APIError) as error:
        raise AiDraftError("imports.aiFailed") from error

    usage = response.usage
    tokens = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise AiDraftError("imports.aiTooLong")
    try:
        body = json.loads(choice.message.content or "")
    except ValueError as error:
        raise AiDraftError("imports.aiFailed") from error
    if not isinstance(body, dict):
        raise AiDraftError("imports.aiFailed")

    result = {"summary": str(body.get("summary") or "")}
    given = _numbers_in(text)
    for sheet in restaurant.sheets:
        rows = [row for row in body.get(sheet.key) or [] if isinstance(row, dict)]
        cleaned, estimated = _clean(rows, sheet, given)
        result[sheet.key] = list(zip(cleaned, estimated))
    return result, tokens
