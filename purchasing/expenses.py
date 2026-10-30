"""
Business expenses with VAT (VAT plan §12): rent, utilities, equipment and
other costs paid outside purchase orders. The VAT rules are the supplier
documents' (purchasing/vat.py); this adds what only expenses have:

  flags()        the supplier-document flags, plus notClaimable,
                 noAttachment and possibleDuplicate
  void()         a wrong expense is voided with a reason, never deleted;
                 logged in the VAT audit log
  why_not_claimed()  why an expense claims 0, for the form

Voided expenses stay listed (struck through) and count nowhere.
"""

from collections import Counter

from django.db import transaction
from django.utils import timezone

from . import vat as purchase_vat
from .models import Expense, SupplierDocument

MIN_VOID_REASON = 10

# For the CSV and the archive index, which are read outside the app (in English).
CATEGORY_LABELS = {"rent": "Rent", "utilities": "Utilities", "equipment": "Equipment", "maintenance": "Maintenance",
                   "services": "Services", "marketing": "Marketing", "other": "Other"}
REASON_LABELS = {"personal_use": "Personal use", "entertainment": "Entertainment / hospitality", "gifts": "Gifts",
                 "passenger_car": "Passenger car (non-commercial)", "staff_benefits": "Staff personal benefits",
                 "other": "Other"}


def _key(supplier_id, number, kind):
    return supplier_id, (number or "").strip().lower(), kind


def duplicate_keys(branch):
    """(supplier, number, kind) keys used by more than one non-voided
    expense or supplier document of the branch: the same paper keyed in
    twice, or an expense that is also on a purchase order."""
    counts = Counter()
    for row in Expense.objects.filter(branch=branch, voided=False).values_list("supplier_id", "number", "kind"):
        counts[_key(*row)] += 1
    for row in (SupplierDocument.objects.filter(purchase__branch=branch, purchase__is_deleted=False)
                .values_list("purchase__supplier_id", "number", "kind")):
        counts[_key(*row)] += 1
    return {key for key, count in counts.items() if count > 1}


def flags(expense, duplicates=None):
    """What a person should look at on an expense. `duplicates` is
    duplicate_keys() when checking many at once."""
    result = purchase_vat.document_flags(expense)
    if expense.not_claimable:
        result.append(purchase_vat.NOT_CLAIMABLE)
    if not expense.attachment_url:
        result.append(purchase_vat.NO_ATTACHMENT)
    if duplicates is None:
        duplicates = duplicate_keys(expense.branch)
    if not expense.voided and _key(expense.supplier_id, expense.number, expense.kind) in duplicates:
        result.append(purchase_vat.POSSIBLE_DUPLICATE)
    return result


def needs_review(flag_list):
    return any(flag in purchase_vat.REVIEW_FLAGS for flag in flag_list)


def why_not_claimed(expense):
    """Why claimable VAT is 0 although VAT was charged, or None."""
    if not expense.vat:
        return None
    if expense.voided:
        return "voided"
    if not expense.supplier_vat_registered:
        return "notRegistered"
    if not expense.supplier_vat_number:
        return "supplierVatMissing"
    if expense.not_claimable:
        return "notClaimable"
    return None


class VoidRejected(Exception):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status
        self.body = body


def void(expense, actor, reason):
    """Voids `expense` (with `reason`, at least MIN_VOID_REASON characters)
    and logs it. In a filed period this needs the owner's lock override
    (raised by the save as PeriodLocked otherwise)."""
    from vat_returns.models import VatAuditLog, VatPeriod
    from zatca.models import ZatcaSettings

    reason = str(reason or "").strip()
    if len(reason) < MIN_VOID_REASON:
        raise VoidRejected(400, {"reason": "error.vatVoidReasonShort"})
    if expense.voided:
        raise VoidRejected(409, {"error": "error.vatAlreadyVoided"})
    with transaction.atomic():
        expense.voided, expense.voided_reason = True, reason
        expense.voided_by, expense.voided_at = actor, timezone.now()
        expense._lock_action = "void"
        try:
            expense.save()
        finally:
            expense._lock_action = None
        settings = ZatcaSettings.objects.filter(branch=expense.branch).first()
        period = (VatPeriod.objects.filter(branch=expense.branch, environment=settings.environment,
                                           start_date__lte=expense.claim_date, end_date__gte=expense.claim_date).first()
                  if settings else None)
        VatAuditLog.objects.create(
            branch=expense.branch, period=period, actor=actor, action="expense_voided",
            summary=f"Expense {expense.number} voided ({expense.supplier.name_en or expense.supplier.name_ar})",
            data={"expense": expense.pk, "number": expense.number, "vat": expense.vat, "reason": reason},
        )
    return expense
