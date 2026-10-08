"""
Shared query-parameter helpers for list endpoints.

All list filtering lives server-side: a client that filters locally has to
download every row first, which is fine for twenty tables and ruinous for a
purchase history that grows every day. It is also subtly wrong the moment a
list is ever truncated — the filter then searches only what happened to be
downloaded and silently reports "no results" for rows that do exist.

These helpers keep the semantics identical across endpoints so "all" always
means no filter, "none" always means "the field is empty", and a blank value
is always ignored rather than matching nothing.
"""

from django.db.models import Q


def text_filter(queryset, params, param, fields):
    """
    Case-insensitive OR-search of `param` across `fields`.

    Mirrors the frontend's old matchesSearch(): a blank or missing value is a
    no-op, not a filter that matches nothing.
    """
    value = (params.get(param) or "").strip()
    if not value:
        return queryset
    condition = Q()
    for field in fields:
        condition |= Q(**{f"{field}__icontains": value})
    return queryset.filter(condition)


def choice_filter(queryset, params, param, field=None, allowed=None):
    """
    Exact match, with "all" (or blank/missing) meaning no filter.

    `allowed` guards against a typo'd value silently emptying the list — an
    unrecognised choice is ignored rather than filtered on.
    """
    value = (params.get(param) or "").strip()
    if not value or value == "all":
        return queryset
    if allowed is not None and value not in allowed:
        return queryset
    return queryset.filter(**{field or param: value})


def fk_filter(queryset, params, param, field=None):
    """
    Filter by a related id, where "none" means "no relation set".

    The frontend's supplier filter already used that convention, so it is kept
    rather than inventing a second one.
    """
    value = (params.get(param) or "").strip()
    field = field or param
    if not value or value == "all":
        return queryset
    if value == "none":
        return queryset.filter(**{f"{field}__isnull": True})
    if not value.isdigit():
        return queryset
    return queryset.filter(**{field: int(value)})


def bool_filter(queryset, params, param, field=None):
    """`?x=true|false`; anything else (including blank) is ignored."""
    value = (params.get(param) or "").strip().lower()
    if value in ("1", "true", "yes"):
        return queryset.filter(**{field or param: True})
    if value in ("0", "false", "no"):
        return queryset.filter(**{field or param: False})
    return queryset


def numeric_range_filter(queryset, params, field, min_param, max_param):
    """Inclusive min/max on a numeric column. Unparseable values are ignored."""
    for param, lookup in ((min_param, "gte"), (max_param, "lte")):
        raw = (params.get(param) or "").strip()
        if not raw:
            continue
        try:
            queryset = queryset.filter(**{f"{field}__{lookup}": float(raw)})
        except (TypeError, ValueError):
            continue
    return queryset
