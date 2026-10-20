"""
The POS side of ZATCA: turning settlements into invoices and cancellations
into credit notes, without ever getting in the way of a payment.

One invoice per settlement. A settlement is the moment money completes:
  - an order paid in full at the till (orders.views.OrderPaymentsView), or
  - a bill settled by a cashier or by Moyasar (orders.billing.mark_bill_paid),
covering exactly the orders that became fully paid in it.

The rules that keep the POS safe:
  - the payment code only *registers* the work (on_commit); nothing here
    runs inside the payment's transaction, so ZATCA can never roll back,
    slow down or fail a payment;
  - every entry point catches everything and logs it; whatever didn't happen
    (process died, no device yet, encryption key missing) is picked up by
    the retry job (`manage.py zatca_retry_queue`), which is the guarantee;
  - an order is invoiced at most once and credited at most once: the
    ZatcaDocumentOrder unique constraint, inside the same transaction as
    the chain advance, makes a race between the hook and the job harmless.

B2C invoices are reported from a background thread. B2B invoices are
cleared right away with a short timeout, because the buyer's invoice is
only valid once cleared; if ZATCA is slow or down the receipt says
"Pending clearance" and the retry job clears it later.
"""

import logging
import threading
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from . import documents, ubl, vat
from .client import ZatcaClient
from .models import (
    DocumentStatus,
    ZatcaDocument,
    ZatcaDocumentOrder,
    ZatcaOrderBuyer,
    ZatcaSettings,
)

log = logging.getLogger("zatca.pos")

# Clearance while the cashier waits: give up fast, the job retries.
CLEARANCE_AT_PAYMENT_TIMEOUT = (4, 8)
_ZERO = Decimal("0.00")

# How each tender reaches ZATCA (BR-KSA-16: 10 cash, 30 credit transfer,
# 42 bank account, 48 bank card, 1 not defined).
_TILL_MEANS = {"cash": ubl.PaymentMeans.CASH, "card": ubl.PaymentMeans.BANK_CARD, "other": ubl.PaymentMeans.NOT_DEFINED}
# Moyasar source type/company -> (receipt label, payment means).
_MOYASAR_MEANS = {
    "mada": ("mada", ubl.PaymentMeans.BANK_CARD),
    "visa": ("visa", ubl.PaymentMeans.BANK_CARD),
    "master": ("mastercard", ubl.PaymentMeans.BANK_CARD),
    "mastercard": ("mastercard", ubl.PaymentMeans.BANK_CARD),
    "amex": ("amex", ubl.PaymentMeans.BANK_CARD),
    "applepay": ("applepay", ubl.PaymentMeans.BANK_CARD),
    "googlepay": ("googlepay", ubl.PaymentMeans.BANK_CARD),
    "samsungpay": ("samsungpay", ubl.PaymentMeans.BANK_CARD),
    # A wallet transfer, not a card payment.
    "stcpay": ("stcpay", ubl.PaymentMeans.CREDIT),
}


# -- entry points (called from orders/, inside their transactions) -----------


def orders_paid(order_ids, *, actor=None, moyasar=None):
    """Register: these orders were just settled together."""
    order_ids = [order_id for order_id in order_ids if order_id]
    if not order_ids:
        return
    source = moyasar_source(moyasar) if moyasar else None
    actor_id = getattr(actor, "pk", None)
    transaction.on_commit(lambda: _quietly(invoice_orders, order_ids, actor_id=actor_id, moyasar=source))


def order_cancelled(order_id, *, actor=None):
    """Register: a (possibly paid and invoiced) order was just cancelled."""
    transaction.on_commit(lambda: _quietly(credit_cancelled_order, order_id))


def order_refunded(refund_id):
    """Register: some of a paid order's lines were just refunded."""
    transaction.on_commit(lambda: _quietly(credit_refund, refund_id))


def _quietly(function, *args, send=True, **kwargs):
    try:
        document = function(*args, **kwargs)
    except Exception:  # never let ZATCA reach the payment's response
        log.exception("ZATCA %s failed for %s; the retry job will pick it up", function.__name__, args)
        return None
    if document is not None and send:
        dispatch(document)
    return document


# -- issuing ---------------------------------------------------------------


def invoice_orders(order_ids, *, actor_id=None, moyasar=None, source="pos"):
    """Issue one invoice for the given orders' settlement, or None when
    there's nothing to invoice (ZATCA off, already invoiced, nothing paid)."""
    from orders.models import Order

    orders = list(Order.objects.filter(pk__in=order_ids).select_related("branch", "table").order_by("pk"))
    if not orders:
        return None
    branch = orders[0].branch
    settings = ZatcaSettings.objects.filter(branch=branch).first()
    if settings is None or not settings.enabled:
        return None

    with transaction.atomic():
        locked = list(Order.objects.select_for_update().filter(pk__in=[o.pk for o in orders]).order_by("pk"))
        invoiced = set(ZatcaDocumentOrder.objects.filter(order__in=locked, kind=ubl.DocumentKind.INVOICE)
                       .values_list("order_id", flat=True))
        eligible = [order for order in orders if order.pk not in invoiced and _is_settled(order)]
        sale = _sale_for(eligible)
        if not sale["lines"]:
            return None
        buyer_row = ZatcaOrderBuyer.objects.filter(order__in=eligible).select_related("buyer").first()
        buyer = buyer_row.buyer if buyer_row else None
        payments = _payments_of(eligible, moyasar)
        totals = vat.calculate(sale["lines"], standard_rate=settings.default_vat_rate,
                               prices_include_vat=settings.prices_include_vat)
        means = tuple(dict.fromkeys(p["means"] for p in payments)) or (ubl.PaymentMeans.NOT_DEFINED,)
        try:
            with transaction.atomic():
                document = documents.issue(
                    branch, kind=ubl.DocumentKind.INVOICE, simplified=buyer is None, vat=totals,
                    names=sale["names"], payment_means=means, buyer=_party(buyer), source=source,
                )
                ZatcaDocumentOrder.objects.bulk_create(
                    [ZatcaDocumentOrder(document=document, order=order, kind=ubl.DocumentKind.INVOICE)
                     for order in eligible]
                )
        except IntegrityError:
            log.info("Orders %s were invoiced concurrently; nothing to do", [o.pk for o in eligible])
            return None
        document.buyer = buyer
        document.receipt = _receipt(settings, eligible, sale, totals, payments, buyer)
        document.save(update_fields=["buyer", "receipt"])
    log.info("Invoice %s issued for orders %s", document.number, [o.pk for o in eligible])
    return document


def credit_cancelled_order(order_id, *, source="pos"):
    """Credit note for a cancelled order that was invoiced: everything the
    invoice charged for it, less what partial refunds already credited.
    None when there's nothing (left) to credit."""
    from orders.models import Order, OrderRefundLine, OrderStatus

    order = Order.objects.filter(pk=order_id).select_related("branch").first()
    if order is None or order.status != OrderStatus.CANCELLED:
        return None
    refunded = {}
    for item_id, quantity in OrderRefundLine.objects.filter(refund__order=order).values_list("item_id", "quantity"):
        key = f"{order.pk}-{item_id}"
        refunded[key] = refunded.get(key, 0) + quantity
    reason = (order.cancel_reason or "Order cancelled").strip()[:255]
    return _credit(order, reason=reason, source=source,
                   quantities=lambda line: line["quantity"] - refunded.get(line["key"], 0))


def credit_refund(refund_id, *, source="pos"):
    """Credit note for one partial refund: exactly its lines, or None."""
    from orders.models import OrderRefund

    refund = OrderRefund.objects.filter(pk=refund_id).select_related("order__branch").first()
    if refund is None:
        return None
    wanted = {}
    for line in refund.lines.all():
        key = f"{refund.order_id}-{line.item_id}"
        wanted[key] = wanted.get(key, 0) + line.quantity
    return _credit(refund.order, reason=refund.reason.strip()[:255] or "Refund", source=source, refund=refund,
                   quantities=lambda line: min(wanted.get(line["key"], 0), line["quantity"]))


def _credit(order, *, reason, quantities, source, refund=None):
    """Issue a credit note against the order's invoice for `quantities(line)`
    of each of its invoiced lines — from the invoice's own snapshot, so the
    note credits what was charged, not what the order says now."""
    from orders.models import Order

    settings = ZatcaSettings.objects.filter(branch=order.branch).first()
    if settings is None or not settings.enabled:
        return None
    link = (ZatcaDocumentOrder.objects.filter(order=order, kind=ubl.DocumentKind.INVOICE)
            .select_related("document").first())
    if link is None or link.document.status == DocumentStatus.REJECTED:
        return None  # nothing valid was invoiced, so nothing to credit
    invoice = link.document
    snapshot = invoice.receipt or {}
    invoiced = snapshot.get("sale_lines", {}).get(str(order.pk), [])
    if not invoiced:
        log.error("Invoice %s has no stored lines for order %s; cannot credit it", invoice.number, order.pk)
        return None
    # The receipt's dish lines for this order, in the same order as its sale lines.
    order_items = [item for item in snapshot.get("items", []) if str(item.get("order_id")) == str(order.pk)]

    sale_lines, names, items = [], {}, []
    for index, line in enumerate(invoiced):
        quantity = quantities(line)
        if quantity <= 0:
            continue
        unit_gross = Decimal(line["unit_gross"])
        sale_lines.append(vat.SaleLine(key=line["key"], quantity=quantity, unit_gross=unit_gross,
                                       category=line.get("category", vat.VatCategory.STANDARD)))
        names[line["key"]] = line["name"]
        item = dict(order_items[index]) if index < len(order_items) else {
            "order_id": order.pk, "name_en": line["name"], "name_ar": line["name"], "variant_en": "", "variant_ar": ""}
        item.update(key=line["key"], quantity=quantity, unit_price=str(unit_gross), total=str(unit_gross * quantity))
        items.append(item)
    if not sale_lines:
        return None

    rate = Decimal(snapshot.get("totals", {}).get("rate", settings.default_vat_rate))
    totals = vat.calculate(sale_lines, standard_rate=rate, prices_include_vat=settings.prices_include_vat)
    means = tuple(dict.fromkeys(p["means"] for p in snapshot.get("payments", []))) or (ubl.PaymentMeans.NOT_DEFINED,)
    with transaction.atomic():
        Order.objects.select_for_update().filter(pk=order.pk).first()
        try:
            with transaction.atomic():
                note = documents.issue(
                    order.branch, kind=ubl.DocumentKind.CREDIT_NOTE, simplified=invoice.simplified, vat=totals,
                    names=names, payment_means=means, buyer=_party(invoice.buyer), original=invoice,
                    reason=reason, source=source,
                )
                ZatcaDocumentOrder.objects.create(document=note, order=order, kind=ubl.DocumentKind.CREDIT_NOTE,
                                                  refund=refund)
        except IntegrityError:
            return None
        note.buyer = invoice.buyer
        note.receipt = {
            **{key: snapshot.get(key) for key in ("seller", "buyer", "order_type", "table")},
            "order_numbers": [order.order_number],
            "items": _with_vat_lines(items, totals),
            "payments": [],
            "tip": "0.00",
            "totals": _totals(totals),
            "original": {"number": invoice.number, "issued_at": invoice.issued_at.isoformat()},
            "reason": reason,
            "refund_id": refund.pk if refund else None,
        }
        note.save(update_fields=["buyer", "receipt"])
    log.info("Credit note %s issued for order %s against invoice %s (%s)", note.number, order.pk, invoice.number,
             f"refund {refund.pk}" if refund else "cancelled")
    return note


def debit_note(invoice, *, description, amount, reason, source="pos"):
    """A debit note (383) adding `amount` (VAT inclusive) to an invoice: an
    undercharge put right. One line, in the owner's own words. Raises
    documents.IssueError when the branch can't issue (no active device)."""
    settings = ZatcaSettings.for_branch(invoice.branch)
    snapshot = invoice.receipt or {}
    rate = Decimal(snapshot.get("totals", {}).get("rate", settings.default_vat_rate))
    key = "debit"
    totals = vat.calculate([vat.SaleLine(key=key, quantity=1, unit_gross=amount)], standard_rate=rate,
                           prices_include_vat=settings.prices_include_vat)
    means = tuple(dict.fromkeys(p["means"] for p in snapshot.get("payments", []))) or (ubl.PaymentMeans.NOT_DEFINED,)
    order_ids = list(invoice.order_links.filter(kind=ubl.DocumentKind.INVOICE).values_list("order_id", flat=True))
    with transaction.atomic():
        note = documents.issue(
            invoice.branch, kind=ubl.DocumentKind.DEBIT_NOTE, simplified=invoice.simplified, vat=totals,
            names={key: description}, payment_means=means, buyer=_party(invoice.buyer), original=invoice,
            reason=reason, source=source,
        )
        ZatcaDocumentOrder.objects.bulk_create(
            [ZatcaDocumentOrder(document=note, order_id=order_id, kind=ubl.DocumentKind.DEBIT_NOTE)
             for order_id in order_ids])
        note.buyer = invoice.buyer
        note.receipt = {
            **{field: snapshot.get(field) for field in ("seller", "buyer", "order_type", "table", "order_numbers")},
            "items": _with_vat_lines([{"key": key, "name_en": description, "name_ar": description, "variant_en": "",
                                       "variant_ar": "", "quantity": 1, "unit_price": str(amount),
                                       "total": str(amount)}], totals),
            "payments": [],
            "tip": "0.00",
            "totals": _totals(totals),
            "original": {"number": invoice.number, "issued_at": invoice.issued_at.isoformat()},
            "reason": reason,
        }
        note.save(update_fields=["buyer", "receipt"])
    log.info("Debit note %s issued against invoice %s", note.number, invoice.number)
    return note


# -- sending ---------------------------------------------------------------


def dispatch(document):
    """Send a freshly issued document the way its kind needs."""
    mode = getattr(django_settings, "ZATCA_SUBMIT", "thread")
    if mode == "off":
        return
    if not document.simplified:
        # B2B: clear now so the receipt can carry ZATCA's QR; never long.
        submit_safely(document.pk, timeout=CLEARANCE_AT_PAYMENT_TIMEOUT)
    elif mode == "inline":
        submit_safely(document.pk)
    else:
        threading.Thread(target=_submit_in_thread, args=(document.pk,), daemon=True,
                         name=f"zatca-report-{document.pk}").start()


def _submit_in_thread(document_id):
    close_old_connections()
    try:
        submit_safely(document_id)
    finally:
        close_old_connections()


def submit_safely(document_id, timeout=None):
    """Submit one pending document under a row lock (skip if someone else
    holds it). Never raises; returns the document or None."""
    try:
        with transaction.atomic():
            document = (ZatcaDocument.objects.select_for_update(skip_locked=True)
                        .select_related("device").filter(pk=document_id).first())
            if document is None or document.is_final:
                return document
            client = ZatcaClient(document.environment, timeout=timeout) if timeout else None
            return documents.submit(document, client)
    except Exception:
        log.exception("Submitting ZATCA document %s failed; it stays pending", document_id)
        return None


# -- helpers ---------------------------------------------------------------


def moyasar_source(payload):
    source = (payload or {}).get("source") or {}
    key = str(source.get("company") or source.get("type") or "").lower()
    if str(source.get("type") or "").lower() in ("applepay", "stcpay", "googlepay", "samsungpay"):
        key = str(source["type"]).lower()
    return key


def _is_settled(order):
    from orders.models import OrderStatus, PaymentStatus

    return order.status != OrderStatus.CANCELLED and order.payment_status == PaymentStatus.PAID


def _sale_for(orders):
    from orders.models import OrderItemStatus

    lines, names, per_order, items = [], {}, {}, []
    for order in orders:
        for item in order.items.exclude(status=OrderItemStatus.VOIDED).order_by("pk"):
            if item.quantity <= 0:
                continue
            key = f"{order.pk}-{item.pk}"
            name = _item_name(item)
            lines.append(vat.SaleLine(key=key, quantity=item.quantity, unit_gross=item.price_snapshot))
            names[key] = name
            per_order.setdefault(str(order.pk), []).append(
                {"key": key, "name": name, "quantity": item.quantity, "unit_gross": str(item.price_snapshot),
                 "category": vat.VatCategory.STANDARD})
            items.append({
                "order_id": order.pk, "key": key,
                "name_en": item.name_en_snapshot, "name_ar": item.name_ar_snapshot,
                "variant_en": item.variant_name_en_snapshot, "variant_ar": item.variant_name_ar_snapshot,
                "quantity": item.quantity, "unit_price": str(item.price_snapshot),
                "total": str(item.price_snapshot * item.quantity),
            })
    if sum((line.unit_gross * line.quantity for line in lines), _ZERO) <= _ZERO:
        lines = []  # a free order isn't a sale to invoice
    return {"lines": lines, "names": names, "per_order": per_order, "items": items}


def _item_name(item):
    ar, en = item.name_ar_snapshot.strip(), item.name_en_snapshot.strip()
    name = f"{ar} | {en}" if ar and en and ar != en else (ar or en or "Item")
    variant_ar, variant_en = item.variant_name_ar_snapshot.strip(), item.variant_name_en_snapshot.strip()
    variant = " | ".join(v for v in dict.fromkeys((variant_ar, variant_en)) if v)
    return f"{name} ({variant})" if variant else name


def _payments_of(orders, moyasar):
    payments = []
    for order in orders:
        for payment in order.payments.all().order_by("paid_at", "pk"):
            method = payment.method
            means = _TILL_MEANS.get(method, ubl.PaymentMeans.NOT_DEFINED)
            if moyasar and method == "card" and moyasar in _MOYASAR_MEANS:
                method, means = _MOYASAR_MEANS[moyasar]
            payments.append({"method": method, "means": means, "amount": str(payment.amount),
                             "tip": str(payment.tip or _ZERO)})
    return payments


def _party(buyer):
    if buyer is None:
        return None
    return ubl.Party(
        name=buyer.legal_name, vat_number=buyer.vat_number,
        other_id=buyer.crn, other_id_scheme="CRN",
        address=ubl.Address(street=buyer.street, building_no=buyer.building_no, additional_no=buyer.additional_no,
                            district=buyer.district, city=buyer.city, postal_code=buyer.postal_code,
                            country=buyer.country),
    )


def _order_type(order):
    if order.source == "pos":
        return "dine_in" if order.table_id else "takeaway"
    return order.source  # qr / web


def _totals(totals):
    rate = next((c.rate for c in totals.categories if c.category == vat.VatCategory.STANDARD), _ZERO)
    return {"net": str(totals.net_total), "vat": str(totals.vat_total), "rate": str(rate),
            "inclusive": str(totals.tax_inclusive), "rounding": str(totals.payable_rounding),
            "payable": str(totals.payable)}


def _with_vat_lines(items, totals):
    """Each receipt item with the invoice line(s) it became, net of VAT, as
    the XML states them: a standard invoice prints the unit price excl. VAT,
    the rate and the VAT per line. A dish whose net doesn't divide evenly is
    two lines (e.g. 2 x 8.70 and 1 x 8.69 for 3 x 10.00), so it has two."""
    by_key = {}
    for line in totals.lines:
        by_key.setdefault(line.key, []).append({
            "quantity": line.quantity, "unit_net": str(line.unit_net), "net": str(line.net),
            "rate": str(line.rate), "vat": str(line.vat), "total": str(line.total)})
    return [{**item, "lines": by_key.get(item.get("key"), [])} for item in items]


def _receipt(settings, orders, sale, totals, payments, buyer):
    tables = [order.table.label_en for order in orders if order.table_id]
    return {
        "seller": {
            "name_ar": settings.seller_name_ar, "name_en": settings.seller_name_en,
            "vat_number": settings.vat_number, "other_id": settings.other_id,
            "other_id_scheme": settings.other_id_scheme,
            "address": {field: getattr(settings, field) for field in (
                "street", "building_no", "additional_no", "district", "city", "postal_code", "country")},
            # As it was when issued: a later logo change never alters a past invoice.
            "logo_url": orders[0].branch.logo_url,
        },
        "buyer": buyer.snapshot() if buyer else None,
        "order_type": _order_type(orders[0]),
        "table": tables[0] if tables else "",
        "order_numbers": [order.order_number for order in orders],
        "items": _with_vat_lines(sale["items"], totals),
        "sale_lines": sale["per_order"],
        "payments": payments,
        "tip": str(sum((Decimal(p["tip"]) for p in payments), _ZERO)),
        "totals": _totals(totals),
        "issued_for": timezone.now().isoformat(),
    }
