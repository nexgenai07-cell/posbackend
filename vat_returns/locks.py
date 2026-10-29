"""
Period locks. Filing a VAT return locks its period: a supplier document
whose claim date is inside it can't be created, changed (including moved
out of it) or deleted, unless the owner overrides with a reason. Every
override is written as a VatLockOverride and an audit entry, in the same
transaction as the change.

The guard runs inside SupplierDocument.save() and .delete(), so the API,
the Django admin and the shell all go through it. A view lets the owner
override by wrapping its work in `override_from(request)`, which reads
`lock_override_reason` from the body (or the query string, for DELETE).

Locks follow the branch's current ZATCA environment: filing a practice
(Sandbox) period doesn't lock real supplier documents once in Production.

A supplier document dated in a filed period isn't an edit of it: it is
claimed from the oldest open period instead (claim_date_for()), unless the
owner keeps it in the filed period, which is an override.

Sales (ZatcaDocument) are never blocked: the POS dates them "now" and they
are never edited after signing. A late change of status shows on the
period as "changed since filing" instead (figures.changed_since_filing).
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from accounts.models import StaffRole

from .models import VatAuditLog, VatLockOverride, VatPeriod

MIN_REASON = 10
SUPPLIER_DOCUMENT = "purchasing.SupplierDocument"

_override = ContextVar("vat_lock_override", default=None)


class PeriodLocked(APIException):
    """409: the change touches a filed period. The admin offers the owner
    an override with a reason."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "vat_period_locked"

    def __init__(self, period):
        super().__init__({"error": "error.vatPeriodLocked", "period_id": str(period.pk),
                          "period_start": period.start_date.isoformat(), "period_end": period.end_date.isoformat()})
        self.period = period


class OverrideNotAllowed(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "vat_override_owner_only"

    def __init__(self):
        super().__init__({"error": "error.vatOverrideOwnerOnly"})


def locked_period(branch, day):
    """The filed period of the branch's current environment that contains
    `day`, or None."""
    from zatca.models import ZatcaSettings

    if day is None:
        return None
    settings = ZatcaSettings.objects.filter(branch=branch).first()
    if settings is None:
        return None
    return VatPeriod.objects.filter(branch=branch, environment=settings.environment, locked=True,
                                    start_date__lte=day, end_date__gte=day).first()


def claim_date_for(branch, day):
    """Where a supplier document dated `day` is claimed: `day` itself, or
    when that is in a filed period, the first day after it (and after
    any filed periods straight after it): the oldest open period."""
    period = locked_period(branch, day)
    while period is not None:
        day = period.end_date + timedelta(days=1)
        period = locked_period(branch, day)
    return day


@contextmanager
def override(actor, reason):
    """Lets the changes made inside go through locked periods, logged as
    `actor`'s override with `reason`. The actor must be the owner."""
    token = _override.set({"actor": actor, "reason": reason})
    try:
        yield
    finally:
        _override.reset(token)


@contextmanager
def override_from(request):
    """override() from the request's `lock_override_reason`, if it has one
    (400 when it is shorter than MIN_REASON characters)."""
    data = getattr(request.data, "get", None)
    reason = (data("lock_override_reason") if data else None) or request.query_params.get("lock_override_reason")
    if not reason:
        yield
        return
    reason = str(reason).strip()
    if len(reason) < MIN_REASON:
        raise ValidationError({"lock_override_reason": "error.vatOverrideReasonShort"})
    with override(request.user, reason):
        yield


def _snapshot(document):
    return {
        "number": document.number, "kind": document.kind, "issue_date": document.issue_date,
        "claim_date": document.claim_date, "net": document.net, "vat": document.vat, "total": document.total,
        "vat_category": document.vat_category, "claimable_vat": document.claimable_vat,
    }


@contextmanager
def guard_supplier_document(document, action):
    """Wraps the save ("create" / "update") or delete of a supplier document.
    Checks the claim date it had (from the database) and the one it will
    have; raises PeriodLocked when either is in a filed period and there is
    no override."""
    before, days = None, []
    if document.pk:
        old = type(document).objects.filter(pk=document.pk).first()
        if old is not None:
            before = _snapshot(old)
            days.append(old.claim_date)
    if action != "delete":
        days.append(document.claim_date)
    branch = document.purchase.branch
    periods = {}
    for day in days:
        period = locked_period(branch, day)
        if period is not None:
            periods[period.pk] = period
    if not periods:
        yield
        return

    context = _override.get()
    if context is None:
        raise PeriodLocked(next(iter(periods.values())))
    actor, reason = context["actor"], context["reason"]
    if actor.role != StaffRole.OWNER:
        raise OverrideNotAllowed()

    object_id = document.pk
    with transaction.atomic():
        yield
        object_id = object_id or document.pk
        after = None if action == "delete" else _snapshot(document)
        for period in periods.values():
            VatLockOverride.objects.create(period=period, action=action, object_type=SUPPLIER_DOCUMENT,
                                           object_id=object_id, before=before, after=after, reason=reason,
                                           actor=actor)
            VatAuditLog.objects.create(
                branch=period.branch, period=period, actor=actor, action="lock_override",
                summary=f"{action} supplier {document.kind.replace('_', ' ')} {document.number}",
                data={"object_type": SUPPLIER_DOCUMENT, "object_id": object_id, "before": before, "after": after,
                      "reason": reason},
            )
