"""
What a printed or on-screen tax invoice / credit note shows, as one payload
shared by every print point (POS payment, bills, orders, reprints and the
customer's Moyasar success page).

Everything comes from the document as issued (its receipt snapshot and
signed/cleared XML), never from the order as it is now: a cancelled order's
lines are voided, but its invoice must still print as it was.

The QR is only included when it is valid to show: always for simplified
invoices (signed by us), and for standard ones only once ZATCA has
cleared them — before that the receipt says "Pending clearance".
"""

from zoneinfo import ZoneInfo

from .environments import profile
from .models import DocumentStatus, ZatcaSettings


def payload(document, *, settings=None, public=False):
    settings = settings or ZatcaSettings.objects.filter(branch_id=document.branch_id).first()
    snapshot = document.receipt or {}
    local = document.issued_at.astimezone(ZoneInfo(getattr(settings, "timezone", "") or "Asia/Riyadh"))
    pending_clearance = not document.simplified and document.status == DocumentStatus.PENDING
    show_qr = document.status != DocumentStatus.REJECTED and (document.simplified or document.status == DocumentStatus.CLEARED)
    data = {
        "id": document.pk,
        "uuid": document.uuid,
        "number": document.number,
        "kind": document.kind,
        "simplified": document.simplified,
        "environment": document.environment,
        "is_test": profile(document.environment).is_test,
        "status": document.status,
        "pending_clearance": pending_clearance,
        "rejected": document.status == DocumentStatus.REJECTED,
        "issued_at": document.issued_at,
        "issue_date": local.strftime("%Y-%m-%d"),
        "issue_time": local.strftime("%H:%M:%S"),
        "seller": snapshot.get("seller") or {},
        "buyer": snapshot.get("buyer"),
        "order_type": snapshot.get("order_type", ""),
        "table": snapshot.get("table", ""),
        "order_numbers": snapshot.get("order_numbers", []),
        "items": [{key: item.get(key) for key in (
            "name_en", "name_ar", "variant_en", "variant_ar", "quantity", "unit_price", "total")}
            for item in snapshot.get("items", [])],
        "totals": snapshot.get("totals", {}),
        "payments": [{key: p.get(key) for key in ("method", "amount", "tip")} for p in snapshot.get("payments", [])],
        "tip": snapshot.get("tip", "0.00"),
        "original": snapshot.get("original"),
        "reason": snapshot.get("reason", ""),
        "qr": document.qr if show_qr else "",
        "footer_en": getattr(settings, "receipt_footer_en", ""),
        "footer_ar": getattr(settings, "receipt_footer_ar", ""),
        "print_count": document.print_count,
    }
    if public:
        data.pop("print_count")
    return data


def for_orders(order_ids, *, public=False):
    """Every document covering these orders, invoices before credit notes."""
    from .models import ZatcaDocument

    documents = (ZatcaDocument.objects.filter(order_links__order_id__in=list(order_ids))
                 .distinct().order_by("icv"))
    if public:
        documents = documents.filter(simplified=True)
    documents = list(documents)
    if not documents:
        return []
    settings = ZatcaSettings.objects.filter(branch_id=documents[0].branch_id).first()
    return [payload(document, settings=settings, public=public) for document in documents]
