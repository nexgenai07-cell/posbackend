"""
The invoice archive (plan §11): every sales and purchase document, read
only. Nothing here changes, re-signs or deletes a document.

  sales      ZatcaDocument from the POS (not self-tests) in the branch's
             current environment: invoices, credit and debit notes, B2C/B2B
  purchases  SupplierDocument of non-deleted purchases
  expenses   non-voided business expenses (VAT plan §12); a voided one is
             a keying mistake and counts nowhere

Filters come as query parameters (so a filtered view can be bookmarked) and
are parsed by parse(); the same parsed filter drives the page and a ZIP
export (archive_zip.py). A VAT period filter means exactly what the VAT
returns page counts: sales issued in the period's window (Saudi time),
purchases and expenses whose claim_date is in the period.

Amounts are signed: credit notes negative, as in the dashboard lists and
the ZIP's index.csv.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Q, Sum

from purchasing.models import Expense, ExpenseCategory, SupplierDocument
from zatca.models import DocumentStatus, ZatcaDocument

from . import figures
from .filing import parse_amount
from .models import VatPeriod

SIDES = ("sales", "purchases", "expenses")
PAGE_SIZE = 50
ZERO = Decimal("0.00")
SALES_KINDS = ("invoice", "credit_note", "debit_note")
PURCHASE_KINDS = ("invoice", "credit_note")
CHANNELS = ("b2c", "b2b")
# The filter keys each side understands; anything else is ignored.
KEYS = {
    "sales": ("date_from", "date_to", "period", "kind", "status", "channel", "customer", "number", "amount_min",
              "amount_max", "q"),
    "purchases": ("date_from", "date_to", "period", "kind", "supplier", "number", "amount_min", "amount_max", "q"),
    "expenses": ("date_from", "date_to", "period", "kind", "supplier", "number", "amount_min", "amount_max", "q",
                 "category", "capital_asset"),
}


class FilterInvalid(Exception):
    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


def _day(raw):
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _id(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse(side, params, branch, settings):
    """{key: value} for `side` from query parameters (empty values
    dropped). Raises FilterInvalid({key: error}) for a value that can't be
    read. `period` becomes the VatPeriod of the current environment."""
    values, errors = {}, {}
    for key in KEYS[side]:
        raw = params.get(key)
        if raw in (None, ""):
            continue
        raw = str(raw).strip()
        if key in ("date_from", "date_to"):
            values[key] = _day(raw) or errors.setdefault(key, "error.dateInvalid")
        elif key == "period":
            period = VatPeriod.objects.filter(pk=_id(raw), branch=branch, environment=settings.environment).first()
            values[key] = period or errors.setdefault(key, "error.archivePeriodInvalid")
        elif key == "kind":
            allowed = SALES_KINDS if side == "sales" else PURCHASE_KINDS
            values[key] = raw if raw in allowed else errors.setdefault(key, "error.archiveKindInvalid")
        elif key == "status":
            values[key] = raw if raw in DocumentStatus.values else errors.setdefault(key, "error.archiveStatusInvalid")
        elif key == "channel":
            values[key] = raw if raw in CHANNELS else errors.setdefault(key, "error.archiveChannelInvalid")
        elif key in ("customer", "supplier"):
            values[key] = _id(raw) or errors.setdefault(key, "error.archiveIdInvalid")
        elif key == "category":
            values[key] = raw if raw in ExpenseCategory.values else errors.setdefault(key, "error.archiveCategoryInvalid")
        elif key == "capital_asset":
            flag = {"true": True, "1": True, "false": False, "0": False}.get(raw.lower())
            values[key] = flag if flag is not None else errors.setdefault(key, "error.archiveCapitalAssetInvalid")
        elif key in ("amount_min", "amount_max"):
            amount = parse_amount(raw)
            values[key] = amount if amount is not None else errors.setdefault(key, "error.vatAmountInvalid")
        else:
            values[key] = raw[:100]
    if values.get("date_from") and values.get("date_to") and values["date_from"] > values["date_to"]:
        errors["date_to"] = "error.archiveDateRange"
    if errors:
        raise FilterInvalid(errors)
    return values


def as_json(filters):
    """A parsed filter as stored on an export (period as its id)."""
    return {key: (value.pk if isinstance(value, VatPeriod) else
                  value.isoformat() if isinstance(value, date) else str(value) if isinstance(value, Decimal) else value)
            for key, value in filters.items()}


def _amount_range(queryset, filters):
    if "amount_min" in filters:
        queryset = queryset.filter(total__gte=filters["amount_min"])
    if "amount_max" in filters:
        queryset = queryset.filter(total__lte=filters["amount_max"])
    return queryset


def sales(branch, settings, filters):
    tz = ZoneInfo(settings.timezone)
    queryset = ZatcaDocument.objects.filter(branch=branch, environment=settings.environment, source="pos")
    if "date_from" in filters:
        queryset = queryset.filter(issued_at__gte=datetime.combine(filters["date_from"], time.min, tzinfo=tz))
    if "date_to" in filters:
        queryset = queryset.filter(issued_at__lt=datetime.combine(filters["date_to"] + timedelta(days=1), time.min,
                                                                  tzinfo=tz))
    if "period" in filters:
        start, end = figures.window(filters["period"], settings)
        queryset = queryset.filter(issued_at__gte=start, issued_at__lt=end)
    if "kind" in filters:
        queryset = queryset.filter(kind=filters["kind"])
    if "status" in filters:
        queryset = queryset.filter(status=filters["status"])
    if "channel" in filters:
        queryset = queryset.filter(simplified=filters["channel"] == "b2c")
    if "customer" in filters:
        queryset = queryset.filter(buyer_id=filters["customer"])
    if "number" in filters:
        queryset = queryset.filter(number__icontains=filters["number"])
    queryset = _amount_range(queryset, filters)
    if "q" in filters:
        q = filters["q"]
        queryset = queryset.filter(Q(number__icontains=q) | Q(buyer__legal_name__icontains=q)
                                   | Q(buyer__vat_number__icontains=q) | Q(receipt__order_numbers__icontains=q))
    return queryset.select_related("buyer", "original").order_by("-issued_at", "-id")


def purchases(branch, settings, filters):
    queryset = SupplierDocument.objects.filter(purchase__branch=branch, purchase__is_deleted=False)
    if "date_from" in filters:
        queryset = queryset.filter(issue_date__gte=filters["date_from"])
    if "date_to" in filters:
        queryset = queryset.filter(issue_date__lte=filters["date_to"])
    if "period" in filters:
        period = filters["period"]
        queryset = queryset.filter(claim_date__gte=period.start_date, claim_date__lte=period.end_date)
    if "kind" in filters:
        queryset = queryset.filter(kind=filters["kind"])
    if "supplier" in filters:
        queryset = queryset.filter(purchase__supplier_id=filters["supplier"])
    if "number" in filters:
        queryset = queryset.filter(number__icontains=filters["number"])
    queryset = _amount_range(queryset, filters)
    if "q" in filters:
        q = filters["q"]
        queryset = queryset.filter(Q(number__icontains=q) | Q(purchase__supplier__name_en__icontains=q)
                                   | Q(purchase__supplier__name_ar__icontains=q) | Q(supplier_vat_number__icontains=q))
    return queryset.select_related("purchase__supplier").order_by("-issue_date", "-id")


def expenses(branch, settings, filters):
    queryset = Expense.objects.filter(branch=branch, voided=False)
    if "date_from" in filters:
        queryset = queryset.filter(issue_date__gte=filters["date_from"])
    if "date_to" in filters:
        queryset = queryset.filter(issue_date__lte=filters["date_to"])
    if "period" in filters:
        period = filters["period"]
        queryset = queryset.filter(claim_date__gte=period.start_date, claim_date__lte=period.end_date)
    for key in ("kind", "category", "capital_asset"):
        if key in filters:
            queryset = queryset.filter(**{key: filters[key]})
    if "supplier" in filters:
        queryset = queryset.filter(supplier_id=filters["supplier"])
    if "number" in filters:
        queryset = queryset.filter(number__icontains=filters["number"])
    queryset = _amount_range(queryset, filters)
    if "q" in filters:
        q = filters["q"]
        queryset = queryset.filter(Q(number__icontains=q) | Q(supplier__name_en__icontains=q)
                                   | Q(supplier__name_ar__icontains=q) | Q(supplier_vat_number__icontains=q)
                                   | Q(description__icontains=q))
    return queryset.select_related("supplier").order_by("-issue_date", "-id")


QUERIES = {"sales": sales, "purchases": purchases, "expenses": expenses}


def sign(kind):
    return -1 if kind == "credit_note" else 1


def sales_row(document):
    s = sign(document.kind)
    receipt = document.receipt or {}
    buyer = receipt.get("buyer") or {}
    return {
        "id": document.pk, "number": document.number, "kind": document.kind,
        "channel": "b2c" if document.simplified else "b2b", "issued_at": document.issued_at,
        "status": document.status, "customer": buyer.get("legal_name", ""), "customer_vat": buyer.get("vat_number", ""),
        "original": document.original.number if document.original_id else "",
        "order_numbers": receipt.get("order_numbers", []),
        "net": s * document.net_total if document.net_total is not None else None,
        "vat": s * document.vat_total, "total": s * document.total,
        "has_cleared_xml": bool(document.cleared_xml),
    }


def purchase_row(document):
    s = sign(document.kind)
    supplier = document.purchase.supplier
    return {
        "id": document.pk, "purchase": document.purchase_id, "number": document.number, "kind": document.kind,
        "issue_date": document.issue_date, "claim_date": document.claim_date,
        "supplier": supplier.name_en or supplier.name_ar, "supplier_id": supplier.pk,
        "supplier_vat": document.supplier_vat_number,
        "net": s * document.net, "vat": s * document.vat, "total": s * document.total,
        "claimable_vat": document.claimable_vat, "pdf_url": document.pdf_url, "has_xml": bool(document.xml),
    }


def expense_row(document):
    s = sign(document.kind)
    supplier = document.supplier
    return {
        "id": document.pk, "number": document.number, "kind": document.kind,
        "issue_date": document.issue_date, "claim_date": document.claim_date,
        "supplier": supplier.name_en or supplier.name_ar, "supplier_id": supplier.pk,
        "supplier_vat": document.supplier_vat_number, "category": document.category,
        "description": document.description, "capital_asset": document.capital_asset,
        "not_claimable_reason": document.not_claimable_reason if document.not_claimable else "",
        "net": s * document.net, "vat": s * document.vat, "total": s * document.total,
        "claimable_vat": document.claimable_vat, "attachment_url": document.attachment_url,
        "attachment_name": document.attachment_name,
    }


ROWS = {"sales": sales_row, "purchases": purchase_row, "expenses": expense_row}


def totals(side, queryset):
    """Count and signed sums over the whole filtered list."""
    net_field = "net_total" if side == "sales" else "net"
    vat_field = "vat_total" if side == "sales" else "vat"
    result = {"count": 0, "net": ZERO, "vat": ZERO, "total": ZERO}
    rows = queryset.order_by().values("kind").annotate(n=Sum(net_field), v=Sum(vat_field), t=Sum("total"))
    for row in rows:
        s = sign(row["kind"])
        result["net"] += s * (row["n"] or ZERO)
        result["vat"] += s * (row["v"] or ZERO)
        result["total"] += s * (row["t"] or ZERO)
    result["count"] = queryset.count()
    return result


def page(side, branch, settings, filters, number=1):
    queryset = QUERIES[side](branch, settings, filters)
    summary = totals(side, queryset)
    pages = max(1, -(-summary["count"] // PAGE_SIZE))
    number = min(max(1, number), pages)
    offset = (number - 1) * PAGE_SIZE
    return {"rows": [ROWS[side](document) for document in queryset[offset:offset + PAGE_SIZE]],
            "page": number, "pages": pages, "page_size": PAGE_SIZE, "totals": summary}
