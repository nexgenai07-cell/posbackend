from .models import Customer


def find_or_create_customer(branch, phone, name=""):
    """
    Find-or-create by (branch, phone), restoring a soft-deleted row instead of
    ever inserting a duplicate (the unique_phone_per_branch constraint would
    otherwise reject it) — shared by CustomerViewSet.create() (POS payment
    screen) and the public QR order-creation endpoint, so neither reimplements
    the soft-delete-restore rule on its own.
    """
    phone = str(phone or "").strip()
    existing = Customer.all_objects.filter(branch=branch, phone=phone).first()
    if existing:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.save(update_fields=["is_deleted", "updated_at"])
        return existing, False
    customer = Customer.objects.create(branch=branch, phone=phone, name=name or "")
    return customer, True
