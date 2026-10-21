"""
"Write with AI" on the campaign form: a first draft of the public post
caption and the WhatsApp message, from a product (with its deal, if it has
one) and whatever the staff member jotted down.

The AI only drafts. Nothing here saves anything: the text is put into the
form, staff edit it, and it goes through the same validation and the same
Publish/Prepare steps as text they typed themselves.

One rule is enforced here rather than trusted to the prompt, the same one
imports/ai.py enforces for prices: a number in the draft that appears in
neither the product data nor the staff's notes (a price, a discount, a
time) is reported back as unverified, so the form can tell staff exactly
which figures to check before anything goes out under the restaurant's name.

Reuses imports/ai.py's client and error handling, so it runs on the same
AI_* settings and fails with the same imports.ai* error codes.
"""

import json
import re

from django.conf import settings

from imports import ai as imports_ai
from imports.ai import AiDraftError

MAX_NOTES = 2000
# Room left in the WhatsApp message for the media link and placeholders.
MAX_DRAFT_MESSAGE = 600

PROMPT = """You write marketing text for a restaurant, as a first draft that staff will edit before anything is sent or posted.

Write only what the restaurant data and the staff notes support:
- Prices, discounts, dates, times and quantities come only from the data or the notes, written exactly as given. Never invent an offer, a price, a deadline or a number of any kind.
- Never invent dishes, ingredients or claims ("best in town", "award-winning") that the data does not support.
- If the data includes a deal, the deal price and its days and times are the point of the message.

What to write (leave a field as an empty string when it is not requested):
- "post_caption": the public Facebook/Instagram post. Friendly and short: a hook line, one or two lines about the food or offer, a call to action. It may mix English and Arabic if the notes ask for it; otherwise write it in the language of the notes, or English when there are no notes. At most 3-6 relevant hashtags at the end, never more than 30. At most 2200 characters. Do not use {{...}} placeholders here: nobody in particular reads a public post.
- "message_en" and "message_ar": the WhatsApp message to one customer, in English and in Arabic (natural Gulf-friendly Modern Standard Arabic, not a word-for-word translation). Personal and short, at most 600 characters each. Start with a greeting using {{name}} (for example "Hi {{name}}," / "مرحباً {{name}}،"); {{restaurant}} may be used for the restaurant's name. Do not add a link: the image or video link is added automatically after the message.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "post_caption": {"type": "string"},
        "message_en": {"type": "string"},
        "message_ar": {"type": "string"},
    },
    "required": ["post_caption", "message_en", "message_ar"],
    "additionalProperties": False,
}

# Arabic-Indic and Eastern Arabic-Indic digits, read as 0-9 so "٢٥" in the
# Arabic message is checked against the 25 in the data.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def enabled():
    return imports_ai.enabled()


def _numbers_in(text):
    """Every number in `text`, normalised ("1,400" and "1400" match; "25.00" and "25" match)."""
    found = set()
    for match in _NUMBER.findall((text or "").translate(_DIGITS)):
        value = match.replace(",", "")
        try:
            number = float(value)
        except ValueError:
            continue
        found.add(f"{number:g}")
    return found


def product_context(product):
    """What the AI is told about the product: its own data, nothing else."""
    if product is None:
        return None
    context = {
        "name_en": product.name_en,
        "name_ar": product.name_ar,
        "category": product.category.name_en or product.category.name_ar,
        "description_en": product.description_en,
        "description_ar": product.description_ar,
        "regular_price": f"{product.price.normalize():f}",
        "sizes": [
            {"name": variant.name_en or variant.name_ar, "price": f"{variant.price.normalize():f}"}
            for variant in product.variants.filter(is_active=True)
        ],
    }
    deal = getattr(product, "deal", None)
    if deal is not None:
        context["deal"] = {
            "deal_price": f"{deal.price.normalize():f}",
            "when": [
                {"day": window.get_day_display(), "from": window.start_time.strftime("%H:%M"), "until": window.end_time.strftime("%H:%M")}
                for window in deal.windows.all()
            ],
        }
    return context


def draft(branch, *, product=None, notes="", whatsapp=True, post=True):
    """
    ({post_caption, message_en, message_ar, unverified_numbers}, (prompt
    tokens, completion tokens)), or AiDraftError.

    `whatsapp` / `post` say which texts to write; the others come back empty.
    """
    restaurant = branch.name_en or branch.name_ar
    data = {"restaurant": {"name_en": branch.name_en, "name_ar": branch.name_ar}}
    if product is not None:
        data["product"] = product_context(product)
    wanted = []
    if post:
        wanted.append("post_caption")
    if whatsapp:
        wanted += ["message_en", "message_ar"]

    user = (
        f"Restaurant data (JSON):\n{json.dumps(data, ensure_ascii=False)}\n\n"
        f"Write these fields: {', '.join(wanted)}.\n\n"
        f"Staff notes:\n{notes.strip() or '(none)'}"
    )
    body, tokens = imports_ai.ask(
        imports_ai.client(timeout=120),
        "campaign_draft",
        SCHEMA,
        [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
        extra_body=settings.AI_FAST_EXTRA_BODY,
        temperature=0.7,
    )

    result = {
        "post_caption": str(body.get("post_caption") or "").strip() if post else "",
        "message_en": str(body.get("message_en") or "").strip() if whatsapp else "",
        "message_ar": str(body.get("message_ar") or "").strip() if whatsapp else "",
    }
    if not any(result.values()):
        raise AiDraftError("imports.aiFailed")

    # Numbers the AI wrote that the data and notes don't contain. The
    # restaurant name is included: "Burger 21" is not an invented figure.
    known = _numbers_in(json.dumps(data, ensure_ascii=False)) | _numbers_in(notes) | _numbers_in(restaurant)
    written = set()
    for text in result.values():
        # Hashtags are words, not claims ("#2for1" is fine if "2 for 1" is).
        written |= _numbers_in(re.sub(r"#\w+", " ", text))
    result["unverified_numbers"] = sorted(written - known, key=float)
    return result, tokens
