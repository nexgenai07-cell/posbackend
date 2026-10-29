"""
The history table: one row per period of the branch's current environment,
newest first, with the same figures as each period's cards. Sandbox and
Simulation periods appear only while in that environment (labelled TEST);
in Production only Production periods are listed. Nothing is deleted.

Worked out in one pass, oldest first, carrying the credit forward with the
same rule as figures.credit_in(), so a row always equals its period's cards
(a test checks that) without recomputing every earlier period per row.
"""

from django.db.models import Sum

from . import figures
from . import periods as vat_periods
from .models import VatFiling, VatPayment, VatRefund

ZERO = figures.ZERO

STATUS_LABELS = {"open": "Not filed", "filed": "Filed", "paid": "Paid", "credit": "Credit"}


def rows(branch, settings, branches=None):
    """History rows, newest first."""
    branches = branches or [branch]
    periods = vat_periods.ensure(branch, settings)  # oldest first
    ids = [period.pk for period in periods]
    filings = {filing.period_id: filing for filing in VatFiling.objects.filter(period__in=ids, is_current=True)}
    paid = dict(VatPayment.objects.filter(period__in=ids, voided=False).order_by().values("period")
                .annotate(s=Sum("amount")).values_list("period", "s"))
    refunded = dict(VatRefund.objects.filter(period__in=ids, voided=False).order_by().values("period")
                    .annotate(s=Sum("amount")).values_list("period", "s"))
    references = {}
    for period_id, reference in (VatPayment.objects.filter(period__in=ids, voided=False)
                                 .order_by("paid_on", "id").values_list("period", "sadad_reference")):
        references.setdefault(period_id, []).append(reference)

    result = []
    carried, source = figures.opening(periods[0]) if periods else (ZERO, "none")
    for period in periods:
        filing = filings.get(period.pk)
        output = figures.output_vat(branches, period, settings)
        purchases = figures.input_vat(branches, period)
        due = figures.net_due(output, purchases, period.correction, carried)
        payments = paid.get(period.pk) or ZERO
        owed = filing.zatca_amount if filing else due
        refund_state = None
        if filing and filing.zatca_amount < 0 and filing.credit_treatment == "refund_requested":
            refund_state = "received" if (refunded.get(period.pk) or ZERO) >= -filing.zatca_amount else "requested"
        result.append({
            **vat_periods.as_dict(period, settings),
            "name": vat_periods.label_text(period),
            "output_vat": output,
            "input_vat": purchases,
            "credit_in": carried,
            "credit_source": source,
            "vat_due": due,
            "zatca_amount": filing.zatca_amount if filing else None,
            "paid": payments,
            "balance": max(owed, ZERO) - payments,
            "filed_on": filing.filed_on if filing else None,
            "sadad_references": references.get(period.pk, []),
            "refund_state": refund_state,
            "overdue": period.status in ("open", "filed") and (period.due_date - vat_periods.today(settings)).days < 0,
        })
        if filing is not None:
            carried, source = figures.carried_from_filing(filing), "filed"
        else:
            carried, source = max(ZERO, -due), "estimated"
    result.reverse()
    return result


def status_text(row):
    """English status for the CSV/PDF, e.g. "Credit (refund requested)", "Filed, overdue"."""
    text = STATUS_LABELS.get(row["status"], row["status"])
    if row["refund_state"]:
        text += f" (refund {row['refund_state']})"
    if row["overdue"]:
        text += ", overdue"
    return text
