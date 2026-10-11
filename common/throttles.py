import time

from django.conf import settings
from django.core.cache import cache
from rest_framework.throttling import SimpleRateThrottle


class _RateTools(SimpleRateThrottle):
    """Borrowed only for DRF's rate parsing and client-IP resolution
    (get_ident honours NUM_PROXIES / X-Forwarded-For like every other throttle)."""

    rate = "1/s"


class FailedLookupLimiter:
    """
    Rate-limits FAILED lookups only, per client IP.

    A restaurant's customers often share one wifi IP, so counting every
    request would lock out a whole dining room. Only misses (wrong code,
    wrong phone) are counted; once an IP has used up the scope's rate
    (settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope]) inside the
    window, further lookups from it are refused until the window passes.
    """

    def __init__(self, scope):
        self.scope = scope
        rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope]
        self.limit, self.window = _RateTools().parse_rate(rate)

    def _key(self, request):
        ident = _RateTools().get_ident(request)
        return f"failed-lookup:{self.scope}:{ident}"

    def _recent(self, request):
        now = time.time()
        return [stamp for stamp in cache.get(self._key(request), []) if stamp > now - self.window]

    def is_blocked(self, request):
        return len(self._recent(request)) >= self.limit

    def record_failure(self, request):
        history = self._recent(request)
        history.append(time.time())
        cache.set(self._key(request), history, self.window)
