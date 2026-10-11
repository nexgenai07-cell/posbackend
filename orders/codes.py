"""Order number and tracking-token helpers.

`format_order_number` is the short, human-readable number every screen shows
(ORD-00000020). `make_tracking_token` is the signed, opaque value: it is only
ever handed to a customer's device as a hidden `tracking_token` field and is
never rendered as text.
"""

from django.core import signing


_CODE_SALT = "orders.public-order-code.v1"


def format_order_number(order_id):
    return f"ORD-{int(order_id):08d}" if order_id else ""


def make_tracking_token(order_id):
    # Plain Signer, not signing.dumps(): dumps() embeds a timestamp, so the
    # same order would get a different token on every response.
    return f"ORD-{signing.Signer(salt=_CODE_SALT).sign_object({'id': int(order_id)}, compress=True)}"


# Kept under its old name for any caller still importing it.
make_order_code = make_tracking_token


def resolve_order_code(code):
    """Resolve a signed tracking token or a short order number (ORD-00000020,
    00000020, #20) to an order id, or None."""
    value = str(code or "").strip().lstrip("#")
    if value.upper().startswith("ORD-"):
        value = value[4:]
    # Current deterministic tokens first, then the timestamped ones issued by
    # signing.dumps() before this change (already in customers' URLs).
    for unsign in (signing.Signer(salt=_CODE_SALT).unsign_object, lambda raw: signing.loads(raw, salt=_CODE_SALT)):
        try:
            order_id = int(unsign(value)["id"])
            if order_id > 0:
                return order_id
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            continue
    digits = value.lstrip("0")
    return int(digits) if digits.isdigit() else None
