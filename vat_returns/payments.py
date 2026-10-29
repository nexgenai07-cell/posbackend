"""
SADAD payments and ZATCA refunds of filed periods: recorded and voided by
the owner, never deleted, every action audited. Each one updates the
period's status (filing.refresh_status): Filed -> Paid once the payments
reach ZATCA's amount, and back to Filed when one is voided.

Payments: only for a period filed with an amount to pay (ZATCA amount > 0).
Paying more than is owed is allowed (the balance shows as overpaid): the
owner records what the bank actually did.

Refunds: only for a period filed with a credit and "refund requested". The
period shows the refund as requested until the refunds reach the credit.
"""

from datetime import date

from django.db import transaction
from django.utils import timezone

from . import figures
from . import periods as vat_periods
from .filing import FilingRejected as Rejected, parse_amount, refresh_status
from .models import CreditTreatment, VatAuditLog, VatPayment, VatRefund

MIN_VOID_REASON = 10


def _date(raw, field, errors, *, earliest, today):
    try:
        value = date.fromisoformat(str(raw or ""))
    except ValueError:
        errors[field] = "error.dateInvalid"
        return None
    if value > today:
        errors[field] = "error.vatDateInFuture"
    elif value < earliest:
        errors[field] = "error.vatDateTooEarly"
    return value


def _common(data, *, date_field, reference_field, attachment_fields, earliest, today):
    """(values, errors) for the fields payments and refunds share."""
    values, errors = {}, {}
    amount = parse_amount(data.get("amount", ""))
    if amount is None or amount <= 0:
        errors["amount"] = "error.vatPositiveAmount"
    values["amount"] = amount
    values[date_field] = _date(data.get(date_field), date_field, errors, earliest=earliest, today=today)
    reference = str(data.get(reference_field) or "").strip()
    if not reference:
        errors[reference_field] = "error.vatReferenceRequired"
    values[reference_field] = reference[:64]
    url_field, name_field = attachment_fields
    url = str(data.get(url_field) or "").strip()
    if url and not url.startswith("https://"):
        errors[url_field] = "error.attachmentUrlInvalid"
    values[url_field] = url[:500]
    values[name_field] = str(data.get(name_field) or "").strip()[:255]
    return values, errors


def record_payment(period, settings, actor, data):
    filing = figures.current_filing(period)
    if filing is None:
        raise Rejected(409, {"error": "error.vatPeriodNotFiled"})
    if filing.zatca_amount <= 0:
        raise Rejected(409, {"error": "error.vatNothingToPay"})
    values, errors = _common(data, date_field="paid_on", reference_field="sadad_reference",
                             attachment_fields=("receipt_url", "receipt_name"),
                             earliest=period.start_date, today=vat_periods.today(settings))
    if errors:
        raise Rejected(400, errors)
    with transaction.atomic():
        payment = VatPayment.objects.create(period=period, recorded_by=actor, **values)
        status = refresh_status(period)
        VatAuditLog.objects.create(
            branch=period.branch, period=period, actor=actor, action="payment_recorded",
            summary=f"Payment {payment.amount} on {payment.paid_on}, SADAD {payment.sadad_reference}",
            data={"payment": payment.pk, **values, "status": status},
        )
    return payment


def record_refund(period, settings, actor, data):
    filing = figures.current_filing(period)
    if filing is None or filing.zatca_amount >= 0 or filing.credit_treatment != CreditTreatment.REFUND_REQUESTED:
        raise Rejected(409, {"error": "error.vatNoRefundRequested"})
    values, errors = _common(data, date_field="received_on", reference_field="reference",
                             attachment_fields=("attachment_url", "attachment_name"),
                             earliest=filing.filed_on, today=vat_periods.today(settings))
    if errors:
        raise Rejected(400, errors)
    with transaction.atomic():
        refund = VatRefund.objects.create(period=period, recorded_by=actor, **values)
        VatAuditLog.objects.create(
            branch=period.branch, period=period, actor=actor, action="refund_recorded",
            summary=f"Refund {refund.amount} received on {refund.received_on}, reference {refund.reference}",
            data={"refund": refund.pk, **values},
        )
    return refund


def void(record, actor, reason):
    """Voids a payment or refund (kept, struck through). Raises Rejected."""
    reason = str(reason or "").strip()
    if len(reason) < MIN_VOID_REASON:
        raise Rejected(400, {"reason": "error.vatVoidReasonShort"})
    kind = "payment" if isinstance(record, VatPayment) else "refund"
    with transaction.atomic():
        record = type(record).objects.select_for_update().get(pk=record.pk)
        if record.voided:
            raise Rejected(409, {"error": "error.vatAlreadyVoided"})
        record.voided, record.voided_reason, record.voided_by, record.voided_at = True, reason, actor, timezone.now()
        record.save(update_fields=["voided", "voided_reason", "voided_by", "voided_at"])
        period = record.period
        status = refresh_status(period)
        VatAuditLog.objects.create(
            branch=period.branch, period=period, actor=actor, action=f"{kind}_voided",
            summary=f"{kind.capitalize()} {record.amount} voided",
            data={kind: record.pk, "amount": record.amount, "reason": reason, "status": status},
        )
    return record


def _voidable(record):
    return {
        "voided": record.voided, "voided_reason": record.voided_reason,
        "voided_by": record.voided_by.name if record.voided_by_id else "", "voided_at": record.voided_at,
        "recorded_by": record.recorded_by.name, "created_at": record.created_at,
    }


def payments_of(period):
    return [{
        "id": payment.pk, "amount": payment.amount, "paid_on": payment.paid_on,
        "sadad_reference": payment.sadad_reference, "receipt_url": payment.receipt_url,
        "receipt_name": payment.receipt_name, "source": payment.source, **_voidable(payment),
    } for payment in VatPayment.objects.filter(period=period).select_related("recorded_by", "voided_by")]


def refunds_of(period):
    return [{
        "id": refund.pk, "amount": refund.amount, "received_on": refund.received_on, "reference": refund.reference,
        "attachment_url": refund.attachment_url, "attachment_name": refund.attachment_name, **_voidable(refund),
    } for refund in VatRefund.objects.filter(period=period).select_related("recorded_by", "voided_by")]
