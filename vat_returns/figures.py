"""
Every number on the VAT returns page, from stored figures only: the cards,
the return boxes and the "Show documents" lists. Nothing here recalculates
VAT from prices; it adds up what the documents store.

Sales (output VAT): ZATCA documents of the period's environment, from the
POS (not self-tests), issued inside the period in Saudi time, with status
Reported or Cleared. Invoices and debit notes add, credit notes subtract
(their amounts are stored positive). Pending and Rejected ones in the same
window are listed separately and not counted.

Purchases (input VAT): supplier documents of non-deleted purchases whose
claim_date falls in the period, adding their stored claimable_vat (negative
for a credit note, 0 when the supplier isn't VAT-registered or has no VAT
number: those are listed as "needing review").

Credit carried in: worked out period by period from the first one, which
starts with its opening credit (owner-entered, usually 0). A filed period
carries what ZATCA confirmed (-zatca_amount when negative and carried
forward; 0 when positive or a refund was requested). A period not filed yet
carries the estimate max(0, -VAT due).

VAT due = output - input + box 14 correction (owner-entered, ±5,000) - credit in.

Once a period is filed, what is still to pay is measured against ZATCA's
amount (what the SADAD bill says) less the SADAD payments recorded, and
the live figures are compared with the filing's snapshot ("changed since
filing").

Return boxes: Amount = invoices, Adjustment = debit notes (+) and credit
notes (-), VAT = both; split by VAT category from the stored breakdown.
(Accountant question A1: whether notes belong in Adjustment.)

`branches` is a list so several branches under one VAT number can be added
together later; today every caller passes one.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, Sum

from purchasing.models import SupplierDocument
from zatca.models import DocumentStatus, ZatcaDocument

from . import boxes as return_boxes
from .models import CreditTreatment, VatFiling, VatPayment, VatPeriod, VatRefund

ZERO = Decimal("0.00")
COUNTED = (DocumentStatus.REPORTED, DocumentStatus.CLEARED)
NOT_COUNTED = (DocumentStatus.PENDING, DocumentStatus.REJECTED)
SIGN = {"invoice": 1, "debit_note": 1, "credit_note": -1}


def window(period, settings):
    """The period as half-open aware datetimes in Saudi time."""
    tz = ZoneInfo(settings.timezone)
    return (datetime.combine(period.start_date, time.min, tzinfo=tz),
            datetime.combine(period.end_date + timedelta(days=1), time.min, tzinfo=tz))


def sales_documents(branches, period, settings):
    """Every POS ZATCA document issued in the period, whatever its status."""
    start, end = window(period, settings)
    return ZatcaDocument.objects.filter(branch__in=branches, environment=period.environment, source="pos",
                                        issued_at__gte=start, issued_at__lt=end)


def purchase_documents(branches, period):
    """Every supplier document claimed in the period."""
    return SupplierDocument.objects.filter(purchase__branch__in=branches, purchase__is_deleted=False,
                                           claim_date__gte=period.start_date, claim_date__lte=period.end_date)


def _signed_vat(rows):
    return sum((SIGN[row["kind"]] * (row["amount"] or ZERO) for row in rows), ZERO)


def output_vat(branches, period, settings):
    rows = (sales_documents(branches, period, settings).filter(status__in=COUNTED)
            .order_by().values("kind").annotate(amount=Sum("vat_total")))
    return _signed_vat(rows)


def input_vat(branches, period):
    return purchase_documents(branches, period).aggregate(vat=Sum("claimable_vat"))["vat"] or ZERO


def current_filing(period):
    return VatFiling.objects.filter(period=period, is_current=True).select_related("filed_by").first()


def paid(period):
    """SADAD payments of the period, voided ones left out."""
    return VatPayment.objects.filter(period=period, voided=False).aggregate(s=Sum("amount"))["s"] or ZERO


def refund(period, filing):
    """For a period filed with a credit and a refund requested: what was
    requested (ZATCA's credit), what has come back so far, and the state
    ("requested" until the refunds add up to it, then "received")."""
    if filing is None or filing.zatca_amount >= 0 or filing.credit_treatment != CreditTreatment.REFUND_REQUESTED:
        return None
    requested = -filing.zatca_amount
    received = VatRefund.objects.filter(period=period, voided=False).aggregate(s=Sum("amount"))["s"] or ZERO
    return {"requested": requested, "received": received, "outstanding": max(requested - received, ZERO),
            "state": "received" if received >= requested else "requested"}


def carried_from_filing(filing):
    """The credit a filed period passes on: ZATCA's confirmed credit, unless
    the owner asked ZATCA to refund it."""
    if filing.zatca_amount < 0 and filing.credit_treatment == CreditTreatment.CARRY_FORWARD:
        return -filing.zatca_amount
    return ZERO


def net_due(output, purchases, correction, credit):
    return output - purchases + correction - credit


def opening(first):
    """(credit into the first period, source): its opening credit."""
    return (first.opening_credit, "opening") if first.opening_credit else (ZERO, "none")


def credit_in(branches, period, settings):
    """(credit carried into `period`, source). Source, from the period just
    before: "none" (first period), "opening" (first period, with an
    opening credit), "filed" (ZATCA's figure) or "estimated" (not filed
    yet: worked out from our figures)."""
    earlier = list(VatPeriod.objects.filter(branch=period.branch, environment=period.environment,
                                            end_date__lt=period.start_date).order_by("start_date"))
    filings = {filing.period_id: filing
               for filing in VatFiling.objects.filter(period__in=earlier, is_current=True)}
    carried, source = opening(earlier[0] if earlier else period)
    for previous in earlier:
        filing = filings.get(previous.pk)
        if filing is not None:
            carried, source = carried_from_filing(filing), "filed"
        else:
            due = net_due(output_vat(branches, previous, settings), input_vat(branches, previous),
                          previous.correction, carried)
            carried, source = max(ZERO, -due), "estimated"
    return carried, source


def _not_counted(branches, period, settings):
    rows = (sales_documents(branches, period, settings).filter(status__in=NOT_COUNTED)
            .order_by().values("status", "kind").annotate(amount=Sum("vat_total"), count=Count("id")))
    result = {status: {"count": 0, "vat": ZERO} for status in NOT_COUNTED}
    for row in rows:
        result[row["status"]]["count"] += row["count"]
        result[row["status"]]["vat"] += SIGN[row["kind"]] * (row["amount"] or ZERO)
    return result


def _empty_box():
    return {"amount": ZERO, "adjustment": ZERO, "vat": ZERO}


def _boxes(branches, period, settings, credit):
    """Box 14 is the period's correction; 15 the credit carried in."""
    values = {number: _empty_box() for number, *_rest in return_boxes.BOXES}
    missing_breakdown = 0
    for kind, breakdown in (sales_documents(branches, period, settings).filter(status__in=COUNTED)
                            .values_list("kind", "vat_breakdown")):
        if breakdown is None:
            missing_breakdown += 1
            continue
        for row in breakdown:
            number = return_boxes.SALES_BOX.get(row["category"])
            if number is None:
                continue
            taxable, vat = Decimal(row["taxable"]), Decimal(row["vat"])
            if kind == "invoice":
                values[number]["amount"] += taxable
            else:
                values[number]["adjustment"] += SIGN[kind] * taxable
            values[number]["vat"] += SIGN[kind] * vat

    claimable = purchase_documents(branches, period).filter(supplier_vat_registered=True).exclude(
        supplier_vat_number="")
    for kind, category, net, vat in claimable.values_list("kind", "vat_category", "net", "claimable_vat"):
        number = return_boxes.PURCHASES_BOX[category]
        if kind == "invoice":
            values[number]["amount"] += net
        else:
            values[number]["adjustment"] -= net
        values[number]["vat"] += vat

    for total, parts in (("6", return_boxes.SALES_BOXES), ("12", return_boxes.PURCHASES_BOXES)):
        for column in ("amount", "adjustment", "vat"):
            values[total][column] = sum((values[part][column] for part in parts), ZERO)
    values["13"]["vat"] = values["6"]["vat"] - values["12"]["vat"]
    values["14"]["vat"] = period.correction
    values["15"]["vat"] = credit
    values["16"]["vat"] = values["13"]["vat"] + values["14"]["vat"] - values["15"]["vat"]

    rows = []
    for number, key, _name, tracked in return_boxes.BOXES:
        box = values[number]
        vat_only = number in return_boxes.VAT_ONLY
        rows.append({"box": number, "key": key, "tracked": tracked, "vat_only": vat_only,
                     "amount": None if vat_only else box["amount"],
                     "adjustment": None if vat_only else box["adjustment"], "vat": box["vat"]})
    return rows, missing_breakdown


def document_ids(branches, period, settings):
    """The documents the figures count, for the filing snapshot."""
    return {
        "output": sorted(sales_documents(branches, period, settings).filter(status__in=COUNTED)
                         .values_list("id", flat=True)),
        "input": sorted(purchase_documents(branches, period).values_list("id", flat=True)),
    }


def compute(period, settings, branches=None, filing=None):
    """The cards, boxes and warnings for one period. `filing` is the
    period's current filing, if any (looked up when not given)."""
    branches = branches or [period.branch]
    filing = filing if filing is not None else current_filing(period)
    output = output_vat(branches, period, settings)
    purchases = input_vat(branches, period)
    credit, credit_source = credit_in(branches, period, settings)
    due = net_due(output, purchases, period.correction, credit)
    payments = paid(period)
    # What is owed: ZATCA's figure once filed, ours until then.
    owed = filing.zatca_amount if filing else due
    boxes, missing_breakdown = _boxes(branches, period, settings, credit)
    review = purchase_documents(branches, period).exclude(supplier_vat_registered=True, supplier_vat_number__gt="")
    review_rows = review.order_by().values("kind").annotate(amount=Sum("vat"), count=Count("id"))
    previous = (VatPeriod.objects.filter(branch=period.branch, environment=period.environment,
                                         end_date__lt=period.start_date).order_by("-start_date").first())
    return {
        "cards": {
            "output_vat": output,
            "input_vat": purchases,
            "credit_in": credit,
            "credit_source": credit_source,
            "previous_period": previous.pk if previous else None,
            "correction": period.correction,
            "vat_due": due,
            "zatca_amount": filing.zatca_amount if filing else None,
            "difference": filing.zatca_amount - due if filing else None,
            "paid": payments,
            # What is still to pay: nothing when the period ends in credit;
            # negative when overpaid.
            "balance": max(owed, ZERO) - payments,
        },
        "refund": refund(period, filing),
        "boxes": boxes,
        "not_counted": _not_counted(branches, period, settings),
        "review": {"count": sum(row["count"] for row in review_rows), "vat": _signed_vat(review_rows)},
        # Counted documents whose VAT split couldn't be read (M1 backfill):
        # in the cards, but missing from the boxes.
        "missing_breakdown": missing_breakdown,
        "changed_since_filing": changed_since_filing(branches, period, settings, filing, output, purchases, credit)
        if filing else None,
    }


def changed_since_filing(branches, period, settings, filing, output, purchases, credit):
    """How the live figures differ from the filing's snapshot: a pending
    sale accepted later, an override on a supplier document, a change in
    the period before. None when nothing moved."""
    snapshot = filing.calculated
    cards = snapshot["cards"]
    deltas = {
        "output_vat": output - Decimal(str(cards["output_vat"])),
        "input_vat": purchases - Decimal(str(cards["input_vat"])),
        "credit_in": credit - Decimal(str(cards["credit_in"])),
    }
    deltas["vat_due"] = deltas["output_vat"] - deltas["input_vat"] - deltas["credit_in"]
    filed_ids = snapshot.get("documents", {})
    live_ids = document_ids(branches, period, settings)
    documents = {}
    for name in ("output", "input"):
        filed, live = set(filed_ids.get(name, [])), set(live_ids[name])
        documents[name] = {"added": sorted(live - filed), "removed": sorted(filed - live)}
    if not any(deltas.values()) and not any(change["added"] or change["removed"] for change in documents.values()):
        return None
    return {**deltas, "documents": documents}


# -- "Show documents" -------------------------------------------------------------

LISTS = ("output", "not_counted", "input", "review")
PAGE_SIZE = 100


def _sales_row(document):
    sign = SIGN[document.kind]
    return {
        "id": document.pk, "number": document.number, "kind": document.kind, "simplified": document.simplified,
        "status": document.status, "issued_at": document.issued_at,
        "buyer_name": document.buyer.legal_name if document.buyer_id else "",
        "original_number": document.original.number if document.original_id else "",
        "net": sign * document.net_total if document.net_total is not None else None,
        "vat": sign * document.vat_total, "total": sign * document.total,
        "has_cleared_xml": bool(document.cleared_xml),
    }


def _purchase_row(document):
    from purchasing import vat as purchase_vat

    sign = -1 if document.kind == "credit_note" else 1
    supplier = document.purchase.supplier
    return {
        "id": document.pk, "purchase": document.purchase_id, "number": document.number, "kind": document.kind,
        "issue_date": document.issue_date, "claim_date": document.claim_date,
        "supplier_name_en": supplier.name_en, "supplier_name_ar": supplier.name_ar,
        "supplier_vat_number": document.supplier_vat_number, "vat_category": document.vat_category,
        "net": sign * document.net, "vat": sign * document.vat, "claimable_vat": document.claimable_vat,
        "flags": purchase_vat.document_flags(document),
    }


def document_list(period, settings, which, page=1, branches=None):
    """One "Show documents" list, a page at a time, with totals over the
    whole list that equal the card it was opened from."""
    branches = branches or [period.branch]
    if which in ("output", "not_counted"):
        qs = sales_documents(branches, period, settings).filter(
            status__in=COUNTED if which == "output" else NOT_COUNTED)
        totals = {"vat": _signed_vat(qs.order_by().values("kind").annotate(amount=Sum("vat_total")))}
        qs = qs.select_related("buyer", "original").order_by("issued_at", "id")
        to_row = _sales_row
    else:
        qs = purchase_documents(branches, period)
        if which == "review":
            qs = qs.exclude(supplier_vat_registered=True, supplier_vat_number__gt="")
        totals = {"claimable_vat": qs.aggregate(v=Sum("claimable_vat"))["v"] or ZERO,
                  "vat": _signed_vat(qs.order_by().values("kind").annotate(amount=Sum("vat")))}
        qs = qs.select_related("purchase__supplier").order_by("claim_date", "id")
        to_row = _purchase_row
    count = qs.count()
    page = max(1, page)
    rows = [to_row(document) for document in qs[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
    return {"list": which, "count": count, "page": page, "page_size": PAGE_SIZE,
            "has_more": page * PAGE_SIZE < count, "totals": totals, "rows": rows}
