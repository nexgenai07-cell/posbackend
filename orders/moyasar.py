"""
The only module that knows the Moyasar secret key exists.

Everything here is server-side. The secret key is read from settings (which
reads it from .env — see config/settings.py's MOYASAR_* block), used as the
username of an HTTP Basic pair with an empty password, and never returned,
serialised or logged. The publishable key is the only one that is ever handed
to a browser, and that happens in billing_views.py, not here.

Switching from test to live is a .env change (sk_test_… -> sk_live_…) and
nothing else — there is no mode flag in this code.
"""

import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger("orders.moyasar")

# Moyasar's own timeouts are generous; ours are not. A customer is standing at
# a table watching a spinner, and the webhook is the safety net if we give up.
_TIMEOUT_SECONDS = (5, 15)  # (connect, read)


class MoyasarNotConfigured(RuntimeError):
    """MOYASAR_SECRET_KEY is blank — online payment is switched off."""


class MoyasarUnreachable(RuntimeError):
    """Network failure or a 5xx from Moyasar. Retryable, unlike a 4xx."""


class MoyasarPaymentNotFound(RuntimeError):
    """Moyasar has no payment with that id (404) — a forged/garbage id."""


def is_configured():
    return bool(settings.MOYASAR_SECRET_KEY)


def publishable_key():
    return settings.MOYASAR_PUBLISHABLE_KEY


def fetch_payment(payment_id):
    """
    GET {MOYASAR_API_BASE}/payments/{id} with HTTP Basic (secret key as the
    username, empty password) and return the parsed JSON.

    This is the ONLY source of truth about whether a payment succeeded. The
    ?status= on the callback redirect and the webhook body are both attacker-
    controllable and are never trusted — they only ever supply the id that
    gets looked up here.
    """
    if not is_configured():
        raise MoyasarNotConfigured("MOYASAR_SECRET_KEY is not set")

    url = f"{settings.MOYASAR_API_BASE.rstrip('/')}/payments/{payment_id}"
    try:
        response = requests.get(
            url,
            auth=(settings.MOYASAR_SECRET_KEY, ""),
            timeout=_TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        # Never interpolate the key or the auth tuple into a log line.
        logger.warning("Moyasar unreachable while verifying %s: %s", payment_id, exc)
        raise MoyasarUnreachable(str(exc)) from exc

    if response.status_code == 404:
        logger.warning("Moyasar has no payment %s", payment_id)
        raise MoyasarPaymentNotFound(payment_id)
    if response.status_code >= 500:
        logger.warning("Moyasar returned %s verifying %s", response.status_code, payment_id)
        raise MoyasarUnreachable(f"Moyasar returned {response.status_code}")
    if response.status_code >= 400:
        # 401 means the secret key is wrong — a configuration error, not a
        # customer error, so it must not be reported as a failed payment.
        logger.error("Moyasar rejected the verify call for %s: %s", payment_id, response.status_code)
        raise MoyasarNotConfigured(f"Moyasar returned {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise MoyasarUnreachable("Moyasar returned a non-JSON body") from exc

    logger.info(
        "Verified Moyasar payment %s: status=%s amount=%s %s",
        payment_id, payload.get("status"), payload.get("amount"), payload.get("currency"),
    )
    return payload


def webhook_secret_matches(payload):
    """
    Moyasar echoes the shared secret you configure in its dashboard back in
    the webhook body's top-level `secret_token` field (alongside `type`,
    `created_at`, `live` and `data`). Compared with hmac.compare_digest so the
    check is constant-time.

    This is defence in depth only. It proves the sender knows the secret, not
    that the body is intact — nothing signs the payload, and the same token
    rides on every event. The real gate is that we re-fetch the payment from
    the API before believing anything (see fetch_payment above), so a webhook
    with a correct token and a lying body still cannot settle a bill.

    Returns True when no secret is configured, i.e. the check is opt-in.
    """
    expected = settings.MOYASAR_WEBHOOK_SECRET
    if not expected:
        return True
    received = payload.get("secret_token")
    if not isinstance(received, str):
        return False
    return hmac.compare_digest(received, expected)
