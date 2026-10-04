from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone
from rest_framework.exceptions import ValidationError


def _parse_date(value, error_code):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError({"error": error_code})


def parse_date_range(request, default_days=30):
    """
    Shared by every report endpoint so ?days=&from=&to= behave identically
    everywhere, per the plan's stated convention. ?from=/?to= (YYYY-MM-DD)
    take precedence when given. ?days=N means "the last N days including
    today". No params at all -> the last `default_days` days.
    """
    branch = getattr(request.user, "branch", None)
    if branch:
        try:
            today = timezone.localdate(timezone=ZoneInfo(branch.timezone))
        except (ZoneInfoNotFoundError, ValueError):
            today = timezone.localdate()
    else:
        today = timezone.localdate()
    preset = request.query_params.get("preset")
    if preset:
        if preset == "today":
            return today, today
        if preset == "yesterday":
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday
        if preset == "this_week":
            return today - timedelta(days=today.weekday()), today
        if preset == "this_month":
            return today.replace(day=1), today
        if preset.startswith("last_") and preset.endswith("_days"):
            try:
                days = max(int(preset[5:-5]), 1)
            except ValueError:
                raise ValidationError({"error": "error.datePresetInvalid"})
            return today - timedelta(days=days - 1), today
        if preset != "custom":
            raise ValidationError({"error": "error.datePresetInvalid"})

    to_param = request.query_params.get("to")
    to_date = _parse_date(to_param, "error.dateInvalid") if to_param else today

    from_param = request.query_params.get("from")
    days_param = request.query_params.get("days")

    if from_param:
        from_date = _parse_date(from_param, "error.dateInvalid")
    elif days_param:
        try:
            days = max(int(days_param), 1)
        except ValueError:
            raise ValidationError({"error": "error.daysInvalid"})
        from_date = to_date - timedelta(days=days - 1)
    else:
        from_date = to_date - timedelta(days=default_days - 1)

    return from_date, to_date


def branch_datetime_bounds(branch, from_date, to_date):
    """Half-open UTC-aware datetime bounds for dates in the branch timezone."""
    try:
        branch_tz = ZoneInfo(branch.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        branch_tz = ZoneInfo("UTC")
    start = datetime.combine(from_date, time.min, tzinfo=branch_tz)
    end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=branch_tz)
    return start, end
