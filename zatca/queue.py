"""
The retry job: the guarantee behind the best-effort payment hook.

run_once() is scheduler-agnostic — Windows Task Scheduler, cron, a
systemd timer or Celery beat just call `manage.py zatca_retry_queue` every
5 minutes. Each run:

  1. invoices orders paid in full while ZATCA was on that have no invoice
     (the process died before the hook ran, no device yet, key missing...);
  2. credits cancelled orders and partial refunds whose invoice has no
     credit note for them yet;
  3. sends every pending document, oldest ICV first (including the ones
     it just issued, in this same process).

Running it twice at once is safe: issuing is guarded by the one-document-
per-order constraint, sending by a skip_locked row lock. Documents are
re-sent unchanged; nothing is ever re-signed.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from . import pos, ubl
from .models import DocumentStatus, ZatcaDocument, ZatcaDocumentOrder, ZatcaSettings

log = logging.getLogger("zatca.queue")

# Leave a document alone this long after an attempt, so the background
# thread that's just sending it isn't raced. Then back off up to an hour.
MIN_WAIT = timedelta(minutes=2)
MAX_WAIT = timedelta(hours=1)


@dataclass
class RunResult:
    issued: list = field(default_factory=list)
    credited: list = field(default_factory=list)
    sent: list = field(default_factory=list)  # (document, status before -> after)
    failed: list = field(default_factory=list)


def run_once(now=None):
    now = now or timezone.now()
    result = RunResult()
    for settings in ZatcaSettings.objects.filter(enabled=True).select_related("branch"):
        if settings.active_device() is None:
            continue
        for order_id in _uninvoiced_orders(settings):
            document = pos._quietly(pos.invoice_orders, [order_id], send=False)
            if document is not None:
                result.issued.append(document)
        for order_id in _uncredited_orders(settings):
            document = pos._quietly(pos.credit_cancelled_order, order_id, send=False)
            if document is not None:
                result.credited.append(document)
        for refund_id in _uncredited_refunds(settings):
            document = pos._quietly(pos.credit_refund, refund_id, send=False)
            if document is not None:
                result.credited.append(document)

    pending = ZatcaDocument.objects.filter(status=DocumentStatus.PENDING).order_by("device_id", "icv")
    for document in pending:
        if not _due(document, now):
            continue
        updated = pos.submit_safely(document.pk)
        if updated is None or updated.status == DocumentStatus.PENDING:
            result.failed.append(updated or document)
        else:
            result.sent.append(updated)
    return result


def _due(document, now):
    if document.last_attempt_at is None:
        return True
    wait = min(MIN_WAIT * (2 ** max(document.attempts - 1, 0)), MAX_WAIT)
    return now - document.last_attempt_at >= wait


def _uninvoiced_orders(settings):
    from orders.models import Order, OrderStatus, PaymentStatus

    if settings.enabled_at is None:
        return []
    return list(
        Order.objects.filter(branch=settings.branch, payment_status=PaymentStatus.PAID)
        .exclude(status=OrderStatus.CANCELLED)
        .exclude(zatca_links__kind=ubl.DocumentKind.INVOICE)
        .annotate(last_paid=Max("payments__paid_at"))
        .filter(last_paid__gte=settings.enabled_at)
        .order_by("last_paid")
        .values_list("pk", flat=True)
    )


def _uncredited_orders(settings):
    from orders.models import OrderStatus

    invoiced = ZatcaDocumentOrder.objects.filter(
        order__branch=settings.branch, order__status=OrderStatus.CANCELLED, kind=ubl.DocumentKind.INVOICE,
    ).exclude(document__status=DocumentStatus.REJECTED).values_list("order_id", flat=True)
    credited = ZatcaDocumentOrder.objects.filter(order_id__in=invoiced, kind=ubl.DocumentKind.CREDIT_NOTE,
                                                 refund__isnull=True).values_list("order_id", flat=True)
    return sorted(set(invoiced) - set(credited))


def _uncredited_refunds(settings):
    """Partial refunds of invoiced orders whose credit note is missing."""
    from orders.models import OrderRefund

    invoiced = ZatcaDocumentOrder.objects.filter(
        order__branch=settings.branch, kind=ubl.DocumentKind.INVOICE,
    ).exclude(document__status=DocumentStatus.REJECTED).values_list("order_id", flat=True)
    return list(OrderRefund.objects.filter(order_id__in=invoiced, zatca_links__isnull=True)
                .order_by("pk").values_list("pk", flat=True))
