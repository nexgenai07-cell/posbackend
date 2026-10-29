"""
Reminder banners: VAT periods that need the owner's attention.

A period is "due soon" from ZatcaSettings.vat_reminder_days before its due
date, and "overdue" after it, while it is Not filed, or Filed but not fully
paid. Paid and Credit periods never remind. Worked out per request (no
scheduler): a quick query finds the candidates, and only then are their
figures worked out.

Only in Production: Sandbox and Simulation periods are practice and would
otherwise remind forever (plan §7.8).
"""

from datetime import timedelta

from zatca.environments import profile

from . import history
from . import periods as vat_periods
from .models import PeriodStatus, VatPeriod

NEEDS_ACTION = (PeriodStatus.OPEN, PeriodStatus.FILED)


def reminders(branch, settings):
    """[{id, label, name, due_date, days_left, level, status, balance}],
    most urgent (earliest due) first."""
    if profile(settings.environment).is_test:
        return []
    vat_periods.ensure(branch, settings)
    today = vat_periods.today(settings)
    horizon = today + timedelta(days=settings.vat_reminder_days)
    candidates = set(VatPeriod.objects.filter(branch=branch, environment=settings.environment,
                                              status__in=NEEDS_ACTION, due_date__lte=horizon)
                     .values_list("id", flat=True))
    if not candidates:
        return []
    result = []
    for row in reversed(history.rows(branch, settings)):  # oldest first
        if row["id"] not in candidates:
            continue
        result.append({
            "id": row["id"], "label": row["label"], "name": row["name"], "due_date": row["due_date"],
            "days_left": row["days_left"], "level": "overdue" if row["days_left"] < 0 else "due_soon",
            "status": row["status"], "balance": row["balance"],
        })
    return result
