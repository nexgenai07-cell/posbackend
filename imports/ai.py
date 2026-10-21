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

Two phases, because a reasoning model writing a whole menu's recipes in one
answer is slow (measured: 4 dishes took ~170 s in one call, ~40 s split):

  1. One quick call with the model's reasoning off (AI_FAST_EXTRA_BODY) for
     the menu and stock rows — copying what the admin wrote needs no
     reasoning — which also names the dishes that need a recipe.
  2. One call per such dish, reasoning on, AI_PARALLEL at a time. A recipe is
     where judgement matters: with reasoning off the model put chicken in a
     mutton karahi.

Talks to any OpenAI-compatible chat endpoint (NVIDIA by default), configured
by AI_API_KEY / AI_BASE_URL / AI_MODEL.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

from django.conf import settings
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from catalog.models import Product
from inventory.models import InventoryItem, RecipeItem

# Values the AI may only copy from the admin's message, never estimate.
NEVER_GUESS = {"price", "size_price", "cost_per_unit", "current_stock", "par_level"}
MAX_TEXT = 20000

MENU_PROMPT = """You turn a restaurant admin's request into rows for their stock and menu, for the admin to review.

The rows are not saved directly. They are checked exactly like an Excel file the admin uploads, and the admin sees every change, with your estimates highlighted, before confirming.

Two kinds of values:
- Money and stock amounts — price, size price, cost per unit, current stock, par level — come only from the admin's message. If the message does not state one, set it to null. Never estimate these.
- Everything else you may decide yourself: a sensible category when none is given, the unit a restaurant would stock an item in, a translation. Every value you decide yourself goes in "estimated_fields" so the admin checks it; values the admin gave you do not.

How to fill the rows:
- Return only rows to add or change. Anything you leave out stays exactly as it is.
- When you mean something that already exists (a stock item, a dish, a category, a supplier), write its name exactly as it appears in the current data.
- Do not write recipes here. Instead list in "recipe_dishes" the name of every dish the admin wants a recipe made or changed for, exactly as the dish is named in your menu rows or the current data. Leave it empty when the admin did not ask for recipes.
- Add a stock row only for an item the admin mentions as stock; ingredients that recipes need are handled separately.
- "summary": one to three short sentences, in the language the admin wrote in, saying what you drafted and anything you could not do.

What each column means:
"""

RECIPE_PROMPT = """You write the recipe a restaurant kitchen uses for ONE portion of a dish, for the admin to review before it is saved.

- List every ingredient the dish really uses, with a realistic quantity for one portion of its regular size. Larger sizes are scaled separately, so do not write quantities for them. Leave out tap water, which is not bought or stocked.
- When an ingredient is one of the restaurant's stock items below, write its name exactly as listed. Otherwise give it a short plain name ("Mutton", "Ginger garlic paste"), the way a shop would sell it.
- "unit" is the unit of the quantity. Use g or ml for anything under 1 kg or 1 l (write 5 g, not 0.005 kg). "stock_unit" is the unit the restaurant would keep the item in stock: rice, flour, meat and vegetables in kg; oil and milk in l; eggs and buns in pcs. For a listed stock item, use its listed unit. The two must fit each other: g or kg for mass, ml or l for volume, pcs or dozen for counted items.
- Follow anything the admin's request says about this dish's recipe."""


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


def menu_schema(restaurant, units):
    stock, menu, _ = restaurant.sheets
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            stock.key: {"type": "array", "items": _row_schema(stock, units)},
            menu.key: {"type": "array", "items": _row_schema(menu, units)},
            "recipe_dishes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", stock.key, menu.key, "recipe_dishes"],
        "additionalProperties": False,
    }


def recipe_schema(units):
    line = {
        "type": "object",
        "properties": {
            "ingredient": {"type": "string"},
            "quantity": {"type": "number"},
            "unit": {"type": "string", "enum": units},
            "stock_unit": {"type": "string", "enum": units},
        },
        "required": ["ingredient", "quantity", "unit", "stock_unit"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"lines": {"type": "array", "items": line}}, "required": ["lines"], "additionalProperties": False}


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


def _menu_instructions(restaurant):
    lines = [MENU_PROMPT]
    for sheet in restaurant.sheets[:2]:
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


def client(timeout=300):
    """The configured chat endpoint. Also used by marketing/ai.py."""
    return OpenAI(base_url=settings.AI_BASE_URL, api_key=settings.AI_API_KEY, timeout=timeout, max_retries=1)


def ask(chat, name, schema, messages, extra_body=None, temperature=0.2):
    """(parsed JSON object, (prompt tokens, completion tokens)) for one call,
    or AiDraftError with an imports.ai* code. Also used by marketing/ai.py."""
    try:
        response = chat.chat.completions.create(
            model=settings.AI_MODEL,
            max_tokens=16000,
            temperature=temperature,
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
            messages=messages,
            extra_body=extra_body or None,
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
    return body, tokens


def draft(restaurant, text):
    """The draft and the token usage, or AiDraftError:
    {"summary", "failed_recipes": [dish], sheet key: [(row dict, [estimated keys])]}.
    A dish whose recipe call fails is reported in failed_recipes rather than
    losing the rest of the draft."""
    chat = client()
    units = restaurant.lists()["Units"]
    current = _current_data(restaurant.branch)
    stock_sheet, menu_sheet, _ = restaurant.sheets

    body, tokens = ask(chat, "restaurant_menu", menu_schema(restaurant, units), [
        {"role": "system", "content": _menu_instructions(restaurant)},
        {"role": "user", "content": f"Current restaurant data (JSON):\n{json.dumps(current, ensure_ascii=False)}\n\nThe admin's request:\n{text}"},
    ], extra_body=settings.AI_FAST_EXTRA_BODY)
    given = _numbers_in(text)
    stock_rows = list(zip(*_clean([r for r in body.get(stock_sheet.key) or [] if isinstance(r, dict)], stock_sheet, given)))
    menu_rows = list(zip(*_clean([r for r in body.get(menu_sheet.key) or [] if isinstance(r, dict)], menu_sheet, given)))

    # The stock the recipes may use: what exists plus what the menu call adds.
    stock_units = {item["name"].casefold(): (item["name"], item["unit"]) for item in current["stock_items"]}
    for values, _ in stock_rows:
        if values.get("name") and values.get("unit"):
            stock_units.setdefault(values["name"].strip().casefold(), (values["name"], values["unit"]))
    stock_list = "\n".join(f"- {name} ({unit})" for name, unit in stock_units.values()) or "(none yet)"
    dishes = list(dict.fromkeys(name.strip() for name in body.get("recipe_dishes") or [] if isinstance(name, str) and name.strip()))

    def recipe(dish):
        return ask(chat, "recipe", recipe_schema(units), [
            {"role": "system", "content": f"{RECIPE_PROMPT}\n\nThe restaurant's stock items (name and stock unit):\n{stock_list}"},
            {"role": "user", "content": f"Dish: {dish}\n\nThe admin's request, for context:\n{text}"},
        ])

    recipe_rows, failed = [], []
    total = list(tokens)
    with ThreadPoolExecutor(max_workers=max(settings.AI_PARALLEL, 1)) as pool:
        futures = [(dish, pool.submit(recipe, dish)) for dish in dishes]
        for dish, future in futures:
            try:
                lines, used = future.result()
            except AiDraftError:
                failed.append(dish)
                continue
            total[0] += used[0]
            total[1] += used[1]
            for line in lines.get("lines") or []:
                if not isinstance(line, dict) or not str(line.get("ingredient") or "").strip():
                    continue
                ingredient = str(line["ingredient"]).strip()
                key = ingredient.casefold()
                if key not in stock_units:
                    # A new ingredient becomes a stock row the admin sees (and
                    # buys); its unit is the AI's call, so marked to check.
                    stock_units[key] = (ingredient, line.get("stock_unit"))
                    stock_rows.append(({
                        "name": ingredient, "unit": line.get("stock_unit"), "current_stock": None,
                        "par_level": None, "cost_per_unit": None, "supplier": None,
                    }, ["unit"]))
                recipe_rows.append((
                    {"product": dish, "ingredient": stock_units[key][0], "quantity": line.get("quantity"), "unit": line.get("unit")},
                    ["quantity", "unit"],
                ))

    result = {
        "summary": str(body.get("summary") or ""),
        "failed_recipes": failed,
        stock_sheet.key: stock_rows,
        menu_sheet.key: menu_rows,
        restaurant.sheets[2].key: recipe_rows,
    }
    return result, tuple(total)
