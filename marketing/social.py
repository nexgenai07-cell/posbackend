"""
Per-restaurant social account connections: storing, checking and reading them.

Every other part of the code gets a connection through get_social_connection()
and a token through page_token(), never by querying SocialConnection or
touching access_token_enc directly. That keeps the two decisions in one place:
which row belongs to a restaurant (today: its Branch), and how a token is
encrypted (SOCIAL_TOKEN_ENCRYPTION_KEY, see common/crypto.py).
"""

import secrets

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from common.crypto import SecretBox

from . import meta
from .models import ConnectionStatus, SocialConnection, SocialProvider

_box = SecretBox("SOCIAL_TOKEN_ENCRYPTION_KEY", label="A social media token")


def encryption_status():
    """(configured, problem) — whether tokens can be stored at all."""
    return _box.status()


def get_social_connection(branch, provider=SocialProvider.META):
    """The restaurant's connection for `provider`, or None."""
    return SocialConnection.objects.filter(branch=branch, provider=provider).first()


def page_token(connection):
    """The decrypted Page token, at the moment it is about to be used."""
    return _box.decrypt(connection.access_token_enc)


def connect_meta(branch, staff, page_id, token):
    """
    Verify a pasted token against Meta and save the Page it gives access to.

    Raises meta.MetaError if Meta rejects it, and EncryptionNotConfigured
    before calling Meta at all if there is no key to store the result with.
    Reconnecting replaces the previous token for the same restaurant.
    """
    # Fail before the network call: a verified token that can't be saved
    # would only make the owner paste it twice.
    _box.encrypt("check")

    info = meta.resolve_page(page_id, token)
    now = timezone.now()
    with transaction.atomic():
        connection = (
            SocialConnection.objects.select_for_update()
            .filter(branch=branch, provider=SocialProvider.META)
            .first()
        ) or SocialConnection(branch=branch, provider=SocialProvider.META)
        _apply(connection, info)
        connection.access_token_enc = _box.encrypt(info.page_token)
        connection.connected_by = staff
        connection.connected_at = now
        connection.last_checked_at = now
        connection.save()
    return connection


def check_connection(connection):
    """
    Ask Meta again whether the stored token still works, refreshing the Page
    name and Instagram account on the way (an Instagram account linked after
    connecting shows up here).

    Never raises for a Meta problem: the outcome is recorded on the row,
    which is what the admin shows. A network failure leaves the status alone,
    since it says nothing about the token.
    """
    try:
        info = meta.resolve_page(connection.page_id, page_token(connection))
    except meta.MetaError as error:
        if error.code == "error.metaUnreachable":
            raise
        connection.status = ConnectionStatus.EXPIRED if error.token_invalid else ConnectionStatus.ERROR
        connection.last_error = error.detail or error.code
    else:
        _apply(connection, info)
    connection.last_checked_at = timezone.now()
    connection.save()
    return connection


# -- "Connect with Facebook" (OAuth) --------------------------------------------
#
# 1. oauth_start: a signed `state` naming the owner, and the dialog URL.
# 2. Facebook sends the browser back to the admin's callback with `code`
#    and `state`; the admin posts both to oauth_pages, which swaps the code
#    for a long-lived user token and lists the owner's Pages.
# 3. The owner picks one; oauth_connect saves it through connect_meta, the
#    same path a pasted token takes.
#
# Between 2 and 3 the user token travels to the browser and back inside a
# signed `session`, Fernet-encrypted first: the browser can carry it but not
# read it, and nothing is stored server-side for a login that is abandoned.

STATE_SALT = "marketing.meta-oauth.state"
SESSION_SALT = "marketing.meta-oauth.session"
# Long enough to log in and approve; short enough that a leaked URL is dead.
STATE_MAX_AGE = 15 * 60
SESSION_MAX_AGE = 15 * 60


class OAuthError(Exception):
    """A login that can't continue. `code` is an errors.json key."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def oauth_configured():
    return bool(settings.META_APP_ID and settings.META_APP_SECRET and settings.META_OAUTH_REDIRECT_URI)


def oauth_start(staff):
    """The Facebook Login URL for this owner."""
    if not oauth_configured():
        raise OAuthError("error.metaOauthNotConfigured")
    _box.encrypt("check")  # EncryptionNotConfigured before a pointless login
    state = signing.dumps(
        {"staff": staff.pk, "branch": staff.branch_id, "nonce": secrets.token_urlsafe(8)}, salt=STATE_SALT
    )
    return meta.oauth_dialog_url(state)


def _load(value, salt, max_age, staff):
    """A signed value this owner was given, or OAuthError."""
    try:
        data = signing.loads(value, salt=salt, max_age=max_age)
    except signing.BadSignature:  # includes SignatureExpired
        raise OAuthError("error.metaOauthExpired") from None
    # Bound to the person who started it: a state or session copied into
    # someone else's browser is refused rather than connecting their Page
    # to the wrong restaurant.
    if data.get("staff") != staff.pk or data.get("branch") != staff.branch_id:
        raise OAuthError("error.metaOauthExpired")
    return data


def oauth_pages(staff, code, state):
    """(pages, session) for the Page picker. Raises OAuthError / MetaError."""
    if not oauth_configured():
        raise OAuthError("error.metaOauthNotConfigured")
    _load(state, STATE_SALT, STATE_MAX_AGE, staff)
    token = meta.exchange_code(code)
    pages = meta.list_pages(token)
    session = signing.dumps(
        {"staff": staff.pk, "branch": staff.branch_id, "token": _box.encrypt(token)}, salt=SESSION_SALT
    )
    return pages, session


def oauth_connect(staff, session, page_id):
    """Connect the picked Page with the token from oauth_pages."""
    data = _load(session, SESSION_SALT, SESSION_MAX_AGE, staff)
    return connect_meta(staff.branch, staff, page_id, _box.decrypt(data.get("token") or ""))


def _apply(connection, info):
    connection.page_id = info.page_id
    connection.page_name = info.page_name
    connection.ig_user_id = info.ig_user_id
    connection.ig_username = info.ig_username
    connection.status = ConnectionStatus.CONNECTED
    connection.last_error = ""
