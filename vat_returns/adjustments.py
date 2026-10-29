"""
Figures the owner enters on a period (plan §7.5, §7.7), each with a reason,
logged, and fixed once the period is filed (they are part of its return):

  opening credit  a VAT credit brought from before this system, on the
                  first period only; carried in like any other credit
  correction      box 14: corrections of earlier periods, within SAR
                  ±5,000 (above that ZATCA expects a voluntary
                  disclosure). Positive adds VAT due, negative lowers it.

Setting 0 removes one; the reason says why.
"""

from decimal import Decimal

from django.db import transaction

from .filing import FilingRejected, parse_amount
from .models import VatAuditLog, VatPeriod

CORRECTION_LIMIT = Decimal("5000.00")
MIN_REASON = 10
# kind: (amount field, reason field, audit summary)
KINDS = {
    "opening_credit": ("opening_credit", "opening_credit_reason", "Opening credit"),
    "correction": ("correction", "correction_reason", "Box 14 correction"),
}


def set_adjustment(period, actor, data):
    """Body: kind (opening_credit | correction), amount, reason. Raises
    FilingRejected (400 for the form, 409 when the period is filed or an
    opening credit isn't on the first period)."""
    kind = data.get("kind")
    if kind not in KINDS:
        raise FilingRejected(400, {"kind": "error.vatAdjustmentKindInvalid"})
    amount_field, reason_field, label = KINDS[kind]

    errors = {}
    amount = parse_amount(data.get("amount", ""))
    if amount is None:
        errors["amount"] = "error.vatAmountInvalid"
    elif kind == "opening_credit" and amount < 0:
        errors["amount"] = "error.vatOpeningCreditNegative"
    elif kind == "correction" and abs(amount) > CORRECTION_LIMIT:
        errors["amount"] = "error.vatCorrectionLimit"
    reason = str(data.get("reason") or "").strip()
    if len(reason) < MIN_REASON:
        errors["reason"] = "error.vatAdjustmentReasonShort"

    with transaction.atomic():
        period = VatPeriod.objects.select_for_update().get(pk=period.pk)
        if period.locked:
            raise FilingRejected(409, {"error": "error.vatAdjustmentPeriodFiled"})
        if kind == "opening_credit" and VatPeriod.objects.filter(
                branch=period.branch, environment=period.environment, start_date__lt=period.start_date).exists():
            raise FilingRejected(409, {"error": "error.vatOpeningCreditFirstOnly"})
        if errors:
            raise FilingRejected(400, errors)
        before = {"amount": getattr(period, amount_field), "reason": getattr(period, reason_field)}
        setattr(period, amount_field, amount)
        setattr(period, reason_field, reason)
        period.save(update_fields=[amount_field, reason_field])
        VatAuditLog.objects.create(
            branch=period.branch, period=period, actor=actor, action=f"{kind}_set",
            summary=f"{label}: {before['amount']} → {amount}",
            data={"before": before, "after": {"amount": amount, "reason": reason}},
        )
    return period
