from .models import Customer


def normalize_phone(phone):
    """Canonicalize Pakistani local and international mobile notation."""
    digits = "".join(character for character in str(phone or "") if character.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = "92" + digits[1:]
    elif digits.startswith("3") and len(digits) == 10:
        digits = "92" + digits
    return digits


def find_or_create_customer(branch, phone, name=""):
    """
    Find-or-create by (branch, phone), restoring a soft-deleted row instead of
    ever inserting a duplicate (the unique_phone_per_branch constraint would
    otherwise reject it) — shared by CustomerViewSet.create() (POS payment
    screen) and the public QR order-creation endpoint, so neither reimplements
    the soft-delete-restore rule on its own.
    """
    phone = normalize_phone(phone)
    existing = next(
        (customer for customer in Customer.all_objects.filter(branch=branch).order_by("pk")
         if normalize_phone(customer.phone) == phone),
        None,
    )
    if existing:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.save(update_fields=["is_deleted", "updated_at"])
        return existing, False
    customer = Customer.objects.create(branch=branch, phone=phone, name=name or "")
    return customer, True
