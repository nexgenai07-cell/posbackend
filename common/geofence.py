from datetime import datetime, timedelta, timezone as dt_timezone
from math import asin, cos, isfinite, radians, sin, sqrt

from django.utils import timezone
from rest_framework.exceptions import ValidationError


EARTH_RADIUS_M = 6_371_000
MAX_FIX_AGE = timedelta(minutes=2)
MAX_FUTURE_SKEW = timedelta(seconds=30)


def _number(value, field, *, minimum, maximum):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "error.locationInvalid"})
    if not isfinite(result) or result < minimum or result > maximum:
        raise ValidationError({field: "error.locationInvalid"})
    return result


def distance_meters(lat1, lon1, lat2, lon2):
    """Haversine great-circle distance, returned in meters."""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(min(1, sqrt(a)))


def validate_branch_location(branch, payload, *, radius_m=None):
    """Reject a missing, invalid, stale, or insufficiently accurate GPS fix.

    The reported accuracy is treated conservatively: the entire uncertainty
    circle must fit inside the configured branch radius.
    """
    if branch.latitude is None or branch.longitude is None:
        raise ValidationError({"location": "error.locationNotConfigured"})

    latitude = _number(payload.get("latitude"), "latitude", minimum=-90, maximum=90)
    longitude = _number(payload.get("longitude"), "longitude", minimum=-180, maximum=180)
    accuracy = _number(payload.get("accuracy_m"), "accuracy_m", minimum=0, maximum=100_000)

    timestamp = payload.get("location_timestamp")
    try:
        observed = datetime.fromtimestamp(float(timestamp), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValidationError({"location_timestamp": "error.locationInvalid"})

    now = timezone.now()
    if observed < now - MAX_FIX_AGE or observed > now + MAX_FUTURE_SKEW:
        raise ValidationError({"location_timestamp": "error.locationStale"})

    distance = distance_meters(
        float(branch.latitude), float(branch.longitude), latitude, longitude
    )
    allowed_radius = branch.geofence_radius_m if radius_m is None else radius_m
    allowed_radius_value = float(allowed_radius)
    minimum_radius = distance + accuracy
    if minimum_radius > allowed_radius_value + 0.01:
        raise ValidationError({
            "location": "error.outsideRestaurantLocation",
            "distance_m": round(distance, 1),
            "accuracy_m": round(accuracy, 1),
            "allowed_radius_m": round(allowed_radius_value, 1),
            "minimum_radius_m": round(minimum_radius, 1),
        })
    return distance
