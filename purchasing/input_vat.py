"""
The input VAT report: one function builds it, and both the JSON endpoint and
the CSV download use it, so the screen and the file can never disagree.

Documents are picked by the supplier document's own date and must belong to
a purchase that hasn't been deleted. Claimable VAT is the stored
`claimable_vat` (purchasing/vat.py); nothing is recalculated here.
"""

from decimal import Decimal

from common.csv_export import money

from . import vat as purchase_vat
from .models import Purchase, PurchaseStatus, SupplierDocument, SupplierDocumentKind

ZERO = Decimal("0.00")


def build(branch, from_date, to_date):
    from reports.filters import branch_datetime_bounds

    documents = (SupplierDocument.objects
                 .filter(purchase__branch=branch, purchase__is_deleted=False,
                         issue_date__gte=from_date, issue_date__lte=to_date)
                 .select_related("purchase__supplier")
                 .prefetch_related("purchase__items")
                 .order_by("issue_date", "id"))
    totals = {"net": ZERO, "vat": ZERO, "total": ZERO, "claimable_vat": ZERO}
    review = {"count": 0, "vat": ZERO}
    rows = []
    for document in documents:
        purchase, supplier = document.purchase, document.purchase.supplier
        sign = -1 if document.kind == SupplierDocumentKind.CREDIT_NOTE else 1
        subtotal = None
        if document.kind == SupplierDocumentKind.INVOICE:
            subtotal = sum((line.quantity * line.unit_cost for line in purchase.items.all()), Decimal("0"))
        flags = purchase_vat.document_flags(document, subtotal)
        needs_review = any(flag in purchase_vat.REVIEW_FLAGS for flag in flags)
        totals["net"] += sign * document.net
        totals["vat"] += sign * document.vat
        totals["total"] += sign * document.total
        totals["claimable_vat"] += document.claimable_vat
        if needs_review:
            review["count"] += 1
            review["vat"] += sign * document.vat
        rows.append({
            "id": document.pk, "kind": document.kind, "purchase": document.purchase_id,
            "supplier": supplier.pk, "supplier_name_en": supplier.name_en, "supplier_name_ar": supplier.name_ar,
            # As saved on the document, not the supplier as it is now.
            "supplier_vat_number": document.supplier_vat_number, "supplier_crn": document.supplier_crn,
            "vat_registered": document.supplier_vat_registered,
            "number": document.number, "issue_date": document.issue_date, "vat_category": document.vat_category,
            "net": sign * document.net, "vat": sign * document.vat, "total": sign * document.total,
            "vat_rate": purchase_vat.vat_rate(document), "claimable_vat": document.claimable_vat,
            "flags": flags, "needs_review": needs_review,
        })

    start, end = branch_datetime_bounds(branch, from_date, to_date)
    missing = []
    for purchase in (Purchase.objects.filter(branch=branch, status=PurchaseStatus.RECEIVED,
                                             received_at__gte=start, received_at__lt=end)
                     .exclude(supplier_documents__kind=SupplierDocumentKind.INVOICE)
                     .select_related("supplier").prefetch_related("items").order_by("received_at")):
        missing.append({
            "purchase": purchase.pk, "supplier": purchase.supplier_id,
            "supplier_name_en": purchase.supplier.name_en, "supplier_name_ar": purchase.supplier.name_ar,
            "vat_registered": purchase.supplier.vat_registered, "received_at": purchase.received_at,
            "subtotal": sum((line.quantity * line.unit_cost for line in purchase.items.all()), Decimal("0")),
            "flags": [purchase_vat.NO_INVOICE] if purchase.supplier.vat_registered
            else [purchase_vat.NO_INVOICE, purchase_vat.NOT_REGISTERED],
        })
    return {"from": from_date, "to": to_date, "rows": rows, "missing_invoices": missing, "totals": totals,
            "review": review}


CSV_COLUMNS = ["Date", "Type", "Supplier", "Supplier VAT number", "Supplier CR", "Document number", "Purchase",
               "VAT category", "Net", "VAT rate %", "VAT", "Total", "Claimable VAT", "Flags"]

_KINDS = {"invoice": "Tax invoice", "credit_note": "Credit note"}
_CATEGORIES = {"standard": "Standard rated", "zero": "Zero rated", "exempt": "Exempt"}


def csv_parts(report, branch):
    """build_csv() keyword arguments for a report: header, rows, totals."""
    from zatca.models import ZatcaSettings

    settings = ZatcaSettings.objects.filter(branch=branch).first()
    name_en = (settings.seller_name_en if settings else "") or branch.name_en
    name_ar = (settings.seller_name_ar if settings else "") or branch.name_ar
    restaurant = " / ".join(name for name in (name_en, name_ar) if name)
    header = [
        ("Restaurant", restaurant),
        ("VAT number", settings.vat_number if settings else ""),
        ("Period", f"{report['from']:%Y-%m-%d} to {report['to']:%Y-%m-%d}"),
        ("Report", "Input VAT (supplier invoices less supplier credit notes; credit notes negative)"),
    ]
    rows = [[
        f"{row['issue_date']:%Y-%m-%d}", _KINDS.get(row["kind"], row["kind"]),
        row["supplier_name_en"] or row["supplier_name_ar"], row["supplier_vat_number"], row["supplier_crn"],
        row["number"], f"PO-{row['purchase']}", _CATEGORIES.get(row["vat_category"], row["vat_category"]),
        money(row["net"]), money(row["vat_rate"]), money(row["vat"]), money(row["total"]),
        money(row["claimable_vat"]),
        "; ".join(purchase_vat.FLAG_LABELS.get(flag, flag) for flag in row["flags"]),
    ] for row in report["rows"]]
    totals = report["totals"]
    totals_row = ["TOTALS", "", "", "", "", "", "", "", money(totals["net"]), "", money(totals["vat"]),
                  money(totals["total"]), money(totals["claimable_vat"]), ""]
    return {"header": header, "columns": CSV_COLUMNS, "rows": rows, "totals": totals_row}
