"""Public order-code helpers. The signature makes new receipt codes opaque."""

from django.core import signing


_CODE_SALT = "orders.public-order-code.v1"


def make_order_code(order_id):
    return f"ORD-{signing.dumps({'id': int(order_id)}, salt=_CODE_SALT, compress=True)}"


def resolve_order_code(code):
    """Resolve signed new codes and legacy numeric codes used by old receipts."""
    value = str(code or "").strip()
    if value.upper().startswith("ORD-"):
        value = value[4:]
    try:
        payload = signing.loads(value, salt=_CODE_SALT)
        order_id = int(payload["id"])
        if order_id > 0:
            return order_id
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        pass
    # Keep old printed receipts and already-open pages usable; phone lookup
    # remains mandatory for these legacy, sequential labels.
    digits = value.lstrip("0")
    return int(digits) if digits.isdigit() else None
