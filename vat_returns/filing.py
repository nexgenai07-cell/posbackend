"""
The ready-to-file checklist and "Mark as filed".

The checklist only warns, it never blocks: filing with open warnings needs
one more confirmation (`confirm_warnings`), and the warnings filed anyway
are kept on the filing.

Filing records what the owner submitted on ZATCA's portal: the date, the net
VAT ZATCA confirmed and, when that differs from our figure, why. It stores a
snapshot of every figure, locks the period and sets its status (Filed, or
Credit when ZATCA's amount is 0 or less).

An amended return (filed again on the portal) is recorded the same way, with
a reason: a new filing that supersedes the current one, which stays listed.
Payments and refunds stay with the period.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from purchasing import vat as purchase_vat
from purchasing.models import Purchase, PurchaseStatus

from . import figures
from . import periods as vat_periods
from .models import CreditTreatment, PeriodStatus, VatAuditLog, VatFiling, VatPeriod

MIN_AMENDMENT_REASON = 10

# Supplier document flags worth a look before filing.
CHECK_FLAGS = {
    purchase_vat.SUPPLIER_VAT, purchase_vat.NOT_REGISTERED, purchase_vat.VAT_NOT_STANDARD, purchase_vat.QR_MISMATCH,
    purchase_vat.QR_UNREADABLE, purchase_vat.NO_QR, purchase_vat.TOTALS,
}
MAX_AMOUNT = Decimal("9999999999.99")


def checklist(period, settings, data, branches=None):
    """[{key, ok, count}] in display order; `data` is figures.compute()."""
    branches = branches or [period.branch]
    start, end = figures.window(period, settings)
    without_invoice = (Purchase.objects.filter(branch__in=branches, status=PurchaseStatus.RECEIVED,
                                               received_at__gte=start, received_at__lt=end)
                       .exclude(supplier_documents__kind="invoice").count())
    expenses = figures.expense_documents(branches, period)
    flagged = sum(1 for document in [*figures.purchase_documents(branches, period), *expenses]
                  if CHECK_FLAGS.intersection(purchase_vat.document_flags(document)))
    previous = (VatPeriod.objects.filter(branch=period.branch, environment=period.environment,
                                         end_date__lt=period.start_date).order_by("-start_date").first())
    previous_open = previous is not None and figures.current_filing(previous) is None
    not_counted = data["not_counted"]
    items = [
        ("pendingDocuments", not_counted["pending"]["count"]),
        ("rejectedDocuments", not_counted["rejected"]["count"]),
        ("purchasesWithoutInvoice", without_invoice),
        ("supplierDocumentsFlagged", flagged),
        ("expensesWithoutAttachment", expenses.filter(attachment_url="").count()),
        ("missingBreakdown", data["missing_breakdown"]),
        ("previousNotFiled", int(previous_open)),
        ("periodNotEnded", int(vat_periods.today(settings) <= period.end_date)),
    ]
    return [{"key": key, "ok": count == 0, "count": count} for key, count in items]


def open_warnings(items):
    return [item["key"] for item in items if not item["ok"]]


class FilingRejected(Exception):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status
        self.body = body


def parse_amount(raw):
    """A signed amount with at most 2 decimals, or None."""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value != value.quantize(Decimal("0.01")) or abs(value) > MAX_AMOUNT:
        return None
    return value.quantize(Decimal("0.01"))


def _parse(period, settings, data, calculated):
    """(values, errors) from the "Mark as filed" form."""
    values, errors = {}, {}
    try:
        filed_on = date.fromisoformat(str(data.get("filed_on") or ""))
    except ValueError:
        errors["filed_on"] = "error.dateInvalid"
    else:
        if filed_on > vat_periods.today(settings):
            errors["filed_on"] = "error.vatFiledOnInFuture"
        elif filed_on < period.start_date:
            errors["filed_on"] = "error.vatFiledOnBeforePeriod"
        values["filed_on"] = filed_on

    amount = parse_amount(data.get("zatca_amount", ""))
    if amount is None:
        errors["zatca_amount"] = "error.vatAmountInvalid"
        return values, errors
    values["zatca_amount"] = amount
    values["difference"] = amount - calculated
    reason = str(data.get("difference_reason") or "").strip()
    if values["difference"] and not reason:
        errors["difference_reason"] = "error.vatDifferenceReasonRequired"
    values["difference_reason"] = reason if values["difference"] else ""

    treatment = data.get("credit_treatment") or CreditTreatment.CARRY_FORWARD
    if amount < 0:
        if treatment not in CreditTreatment.values:
            errors["credit_treatment"] = "error.vatCreditTreatmentInvalid"
        values["credit_treatment"] = treatment
    else:
        values["credit_treatment"] = ""

    url = str(data.get("attachment_url") or "").strip()
    if url and not url.startswith("https://"):
        errors["attachment_url"] = "error.attachmentUrlInvalid"
    values["attachment_url"] = url[:500]
    values["attachment_name"] = str(data.get("attachment_name") or "").strip()[:255]
    return values, errors


def _expected_filing(period, amend):
    """The current filing an amendment replaces (None for a first filing);
    409 when the period isn't in the state the request expects."""
    current = figures.current_filing(period)
    if not amend and current is not None:
        raise FilingRejected(409, {"error": "error.vatPeriodAlreadyFiled"})
    if amend and current is None:
        raise FilingRejected(409, {"error": "error.vatPeriodNotFiled"})
    return current


def file_period(period, settings, actor, data, *, amend=False):
    """Records the filing and locks the period; with `amend`, records an
    amended return that supersedes the current filing (body also needs
    amendment_reason). Raises FilingRejected (400 for the form, 409 when
    the period is already / not yet filed or warnings aren't confirmed)."""
    previous = _expected_filing(period, amend)
    computed = figures.compute(period, settings)
    calculated = computed["cards"]["vat_due"]
    values, errors = _parse(period, settings, data, calculated)
    if amend:
        reason = str(data.get("amendment_reason") or "").strip()
        if len(reason) < MIN_AMENDMENT_REASON:
            errors["amendment_reason"] = "error.vatAmendmentReasonShort"
        values["amendment_reason"] = reason
        if values.get("filed_on") and values["filed_on"] < previous.filed_on:
            errors["filed_on"] = "error.vatAmendedBeforeFiling"
    if errors:
        raise FilingRejected(400, errors)
    warnings = open_warnings(checklist(period, settings, computed))
    if warnings and data.get("confirm_warnings") is not True:
        raise FilingRejected(409, {"error": "error.vatChecklistOpen", "warnings": warnings})

    with transaction.atomic():
        period = VatPeriod.objects.select_for_update().get(pk=period.pk)
        previous = _expected_filing(period, amend)
        if previous is not None:
            # First, so the "one current filing" constraint holds.
            previous.is_current = False
            previous.save(update_fields=["is_current"])
        filing = VatFiling.objects.create(
            period=period, calculated={**computed, "documents": figures.document_ids([period.branch], period, settings)},
            calculated_net_due=calculated, warnings=warnings, filed_by=actor, supersedes=previous, **values,
        )
        period.locked = True
        period.save(update_fields=["locked"])
        refresh_status(period)
        audit = {"filed_on": filing.filed_on, "zatca_amount": filing.zatca_amount,
                 "calculated_net_due": calculated, "difference": filing.difference,
                 "difference_reason": filing.difference_reason, "credit_treatment": filing.credit_treatment,
                 "attachment_url": filing.attachment_url, "warnings": warnings}
        if previous is None:
            action, summary = "filed", f"Filed {period.start_date} – {period.end_date}: ZATCA amount {filing.zatca_amount}"
        else:
            action = "amended"
            summary = (f"Amended {period.start_date} – {period.end_date}: ZATCA amount {previous.zatca_amount} "
                       f"→ {filing.zatca_amount}")
            audit.update(supersedes=previous.pk, previous_zatca_amount=previous.zatca_amount,
                         amendment_reason=filing.amendment_reason)
        VatAuditLog.objects.create(branch=period.branch, period=period, actor=actor, action=action, summary=summary,
                                   data=audit)
    return filing


def refresh_status(period):
    """Sets the period's status from its filing and payments: Not filed;
    Filed (ZATCA amount > 0, not fully paid); Paid; Credit (ZATCA amount
    0 or less: nothing to pay)."""
    filing = figures.current_filing(period)
    if filing is None:
        new = PeriodStatus.OPEN
    elif filing.zatca_amount <= 0:
        new = PeriodStatus.CREDIT
    else:
        new = PeriodStatus.PAID if figures.paid(period) >= filing.zatca_amount else PeriodStatus.FILED
    if period.status != new:
        period.status = new
        period.save(update_fields=["status"])
    return new


def filing_dict(filing):
    if filing is None:
        return None
    return {
        "id": filing.pk, "filed_on": filing.filed_on, "zatca_amount": filing.zatca_amount,
        "calculated_net_due": filing.calculated_net_due, "difference": filing.difference,
        "difference_reason": filing.difference_reason, "credit_treatment": filing.credit_treatment,
        "attachment_url": filing.attachment_url, "attachment_name": filing.attachment_name,
        "warnings": filing.warnings, "filed_by": filing.filed_by.name, "created_at": filing.created_at,
        "is_current": filing.is_current, "supersedes": filing.supersedes_id,
        "amendment_reason": filing.amendment_reason,
    }


def filings_of(period):
    """Every filing of the period, newest first: the current one, then the
    ones it superseded."""
    return [filing_dict(filing) for filing in period.filings.select_related("filed_by")]
