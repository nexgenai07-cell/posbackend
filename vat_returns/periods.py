"""
VAT periods: building them from the branch's VAT filing settings, and the
dates every page shows (due date, days left, label).

  first period   starts on the later of the VAT registration date and the
                 "track VAT returns from" date, and ends at the end of that
                 calendar month or quarter (so it can be short)
  next periods   one calendar month or quarter each, back to back
  due date       the last day of the month after the period ends
  dates          Saudi dates (ZatcaSettings.timezone), end inclusive

Periods are created lazily by ensure(), up to the one containing today.
rebuild() replaces them after a settings change, and refuses once any period
is locked (filed): from then on the past is fixed.

Switching monthly <-> quarterly once a period is filed takes an "effective
from" date: the first day of a calendar quarter after the last period
created (earliest_switch()). Periods before it keep their frequency, so
there are never gaps or overlaps.
"""

import calendar
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from zatca.environments import profile
from zatca.models import VatFilingFrequency, ZatcaSettings

from .models import VatAuditLog, VatPeriod


class PeriodsLocked(Exception):
    """A settings change would rewrite periods that are already filed."""


def today(settings):
    return timezone.localdate(timezone=ZoneInfo(settings.timezone))


def first_start(settings):
    """The first period's first day, or None until a date is set."""
    dates = [d for d in (settings.vat_registration_date, settings.vat_tracking_start) if d]
    return max(dates) if dates else None


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def period_end(start, frequency):
    """The end of the calendar month or quarter containing `start`."""
    if frequency == VatFilingFrequency.MONTHLY:
        return _month_end(start.year, start.month)
    last_month = (start.month - 1) // 3 * 3 + 3
    return _month_end(start.year, last_month)


def natural_start(day, frequency):
    """The first day of the calendar month or quarter containing `day`."""
    if frequency == VatFilingFrequency.MONTHLY:
        return day.replace(day=1)
    return date(day.year, (day.month - 1) // 3 * 3 + 1, 1)


def due_date(end):
    """The last day of the month after the period."""
    year, month = (end.year + 1, 1) if end.month == 12 else (end.year, end.month + 1)
    return _month_end(year, month)


def quarter_start(day):
    """True on 1 Jan, 1 Apr, 1 Jul and 1 Oct."""
    return day == natural_start(day, VatFilingFrequency.QUARTERLY)


def next_quarter_start(day):
    """The first quarter start after `day`."""
    start = natural_start(day, VatFilingFrequency.QUARTERLY)
    year, month = (start.year + 1, 1) if start.month == 10 else (start.year, start.month + 3)
    return date(year, month, 1)


def frequency_for(settings, start, previous):
    """The frequency of a new period starting on `start`: the settings'
    one from its effective date, the previous period's before it."""
    effective = settings.vat_frequency_effective_from
    if previous is None or effective is None or start >= effective:
        return settings.vat_filing_frequency
    return previous


def earliest_switch(periods):
    """The earliest date a frequency change may take effect once periods
    are filed: the first quarter start after the last period created."""
    return next_quarter_start(periods[-1].end_date) if periods else None


def ensure(branch, settings=None):
    """The branch's periods in its current environment, oldest first,
    creating any missing ones up to the period containing today."""
    settings = settings or ZatcaSettings.for_branch(branch)
    periods = list(VatPeriod.objects.filter(branch=branch, environment=settings.environment).order_by("start_date"))
    start = periods[-1].end_date + timedelta(days=1) if periods else first_start(settings)
    if start is None:
        return periods
    now = today(settings)
    new = []
    frequency = periods[-1].frequency if periods else None
    while start <= now:
        frequency = frequency_for(settings, start, frequency)
        end = period_end(start, frequency)
        new.append(VatPeriod(branch=branch, environment=settings.environment, frequency=frequency,
                             start_date=start, end_date=end, due_date=due_date(end)))
        start = end + timedelta(days=1)
    if new:
        # Two requests building the same period at once: the unique
        # constraint keeps one, and the re-read below returns it.
        VatPeriod.objects.bulk_create(new, ignore_conflicts=True)
        periods = list(VatPeriod.objects.filter(branch=branch, environment=settings.environment)
                       .order_by("start_date"))
    return periods


def rebuild(branch, settings, *, actor=None):
    """Replace this environment's periods after the VAT filing settings
    changed. Only allowed while none of them is locked. The owner's
    entries move across: the opening credit to the new first period, a
    box 14 correction to a new period with the same dates."""
    with transaction.atomic():
        existing = VatPeriod.objects.select_for_update().filter(branch=branch, environment=settings.environment)
        if existing.filter(locked=True).exists():
            raise PeriodsLocked()
        old = list(existing.order_by("start_date"))
        replaced = [{"start_date": p.start_date.isoformat(), "end_date": p.end_date.isoformat(),
                     "frequency": p.frequency, "opening_credit": str(p.opening_credit),
                     "correction": str(p.correction)} for p in old]
        existing.delete()
        periods = ensure(branch, settings)
        _carry_entries(old, periods)
        VatAuditLog.objects.create(
            branch=branch, actor=actor, action="periods_rebuilt",
            summary=f"{len(replaced)} periods replaced by {len(periods)}",
            data={"environment": settings.environment, "replaced": replaced,
                  "first_start": periods[0].start_date.isoformat() if periods else None},
        )
    return periods


def label(period):
    """What the page needs to name a period in either language:
    {"year", "quarter" | None, "month" | None, "partial_from" | None}."""
    if period.frequency == VatFilingFrequency.MONTHLY:
        quarter, month = None, period.end_date.month
    else:
        quarter, month = (period.end_date.month - 1) // 3 + 1, None
    partial = period.start_date != natural_start(period.end_date, period.frequency)
    return {"year": period.end_date.year, "quarter": quarter, "month": month,
            "partial_from": period.start_date.isoformat() if partial else None}


def label_text(period):
    """The period's English name for exports: "Q3 2026", "October 2026",
    plus "(from 17 Aug 2026)" for a short first period."""
    parts = label(period)
    name = (f"Q{parts['quarter']} {parts['year']}" if parts["quarter"]
            else period.end_date.strftime("%B %Y"))
    if parts["partial_from"]:
        name += f" (from {period.start_date:%d %b %Y})"
    return name


def period_on(branch, day):
    """{id, label, locked} of the period of the branch's current
    environment that contains `day`, or None (not created yet)."""
    settings = ZatcaSettings.objects.filter(branch=branch).first()
    if settings is None or day is None:
        return None
    period = VatPeriod.objects.filter(branch=branch, environment=settings.environment, start_date__lte=day,
                                      end_date__gte=day).first()
    if period is None:
        return None
    return {"id": period.pk, "label": label(period), "locked": period.locked}


def as_dict(period, settings):
    now = today(settings)
    return {
        "id": period.pk,
        "environment": period.environment,
        "is_test": profile(period.environment).is_test,
        "frequency": period.frequency,
        "label": label(period),
        "start_date": period.start_date,
        "end_date": period.end_date,
        "due_date": period.due_date,
        # Negative once overdue.
        "days_left": (period.due_date - now).days,
        "has_ended": now > period.end_date,
        "is_current": period.start_date <= now <= period.end_date,
        "status": period.status,
        "locked": period.locked,
        "opening_credit": period.opening_credit,
        "opening_credit_reason": period.opening_credit_reason,
        "correction": period.correction,
        "correction_reason": period.correction_reason,
    }


def _carry_entries(old, periods):
    if not periods:
        return
    if old and old[0].opening_credit:
        first = periods[0]
        first.opening_credit, first.opening_credit_reason = old[0].opening_credit, old[0].opening_credit_reason
        first.save(update_fields=["opening_credit", "opening_credit_reason"])
    corrections = {(p.start_date, p.end_date): p for p in old if p.correction}
    for period in periods:
        match = corrections.get((period.start_date, period.end_date))
        if match is not None:
            period.correction, period.correction_reason = match.correction, match.correction_reason
            period.save(update_fields=["correction", "correction_reason"])
