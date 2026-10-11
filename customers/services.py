from .models import Customer

# Arabic-Indic (U+0660–0669) and Extended Arabic-Indic / Persian (U+06F0–06F9)
# digits, mapped to ASCII. Customers on Arabic keyboards type these.
_DIGIT_MAP = {**{0x0660 + i: str(i) for i in range(10)}, **{0x06F0 + i: str(i) for i in range(10)}}

# Mobile numbers are compared on their last 9 digits: that is the subscriber
# number in both Saudi (05x xxx xxxx -> 5xxxxxxxx) and Pakistani
# (03xx xxxxxxx -> 3xxxxxxxxx keeps 9 of its 10) notation, whatever prefix
# (0, +966, 966, 00966, +92 ...) the customer typed.
PHONE_MATCH_DIGITS = 9


def normalize_phone(phone):
    """ASCII digits only, Arabic/Persian digits converted, a leading "00" dropped."""
    digits = "".join(ch for ch in str(phone or "").translate(_DIGIT_MAP) if ch.isascii() and ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def phone_key(phone):
    """The comparable part of a phone number, or "" when it is too short to match."""
    digits = normalize_phone(phone)
    return digits[-PHONE_MATCH_DIGITS:] if len(digits) >= PHONE_MATCH_DIGITS else ""


def phones_match(first, second):
    key = phone_key(first)
    return bool(key) and key == phone_key(second)


def find_or_create_customer(branch, phone, name=""):
    """
    Find-or-create by (branch, phone), restoring a soft-deleted row instead of
    ever inserting a duplicate (the unique_phone_per_branch constraint would
    otherwise reject it) — shared by CustomerViewSet.create() (POS payment
    screen) and the public QR order-creation endpoint, so neither reimplements
    the soft-delete-restore rule on its own.

    Matching is by phones_match(), so "+966 50 123 4567" finds the customer who
    first typed "0501234567". A new customer keeps the phone as they typed it.
    """
    raw = str(phone or "").strip()
    digits = normalize_phone(raw)
    existing = next(
        (customer for customer in Customer.all_objects.filter(branch=branch).order_by("pk")
         # Exact digits as a fallback, so a number too short to phone_key()
         # still finds its own row instead of tripping the unique constraint.
         if phones_match(customer.phone, raw) or (digits and normalize_phone(customer.phone) == digits)),
        None,
    )
    if existing:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.save(update_fields=["is_deleted", "updated_at"])
        return existing, False
    customer = Customer.objects.create(branch=branch, phone=raw, name=name or "")
    return customer, True
