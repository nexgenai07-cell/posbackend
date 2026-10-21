"""
The few Meta Graph API calls a social connection needs.

Kept apart from senders.py: these calls are about *which account* a token
belongs to, made once when the owner connects and again on "Check again",
not per message. Posting to the Page and Instagram will build on the same
request and error handling (graph_post, used by publishing.py).

The token is passed in by the caller and never logged; error messages carry
Meta's code and message, which don't include it.
"""

import hashlib
import hmac
import logging
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 15

# Graph error codes that mean the token itself is no good: 190 is "invalid
# OAuth access token" (expired, revoked, password changed), 102 is a session
# problem with the same fix. Anything else is a problem with the request.
TOKEN_ERROR_CODES = {102, 190}


class MetaError(Exception):
    """A Graph call failed. `code` is an error code the admin can translate."""

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code
        # Meta's own message, kept short, for staff to read when the code
        # alone doesn't explain it (e.g. a missing permission).
        self.detail = detail[:200]

    @property
    def token_invalid(self):
        return self.code == "error.metaTokenInvalid"


class PageInfo:
    """What a token turned out to give access to."""

    __slots__ = ("page_id", "page_name", "page_token", "ig_user_id", "ig_username")

    def __init__(self, page_id, page_name, page_token, ig_user_id="", ig_username=""):
        self.page_id = page_id
        self.page_name = page_name
        self.page_token = page_token
        self.ig_user_id = ig_user_id
        self.ig_username = ig_username


def _url(path):
    return f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{path.lstrip('/')}"


def _auth_params(token):
    params = {"access_token": token}
    # appsecret_proof proves the call comes from this server and not from
    # someone holding a leaked token. Meta requires it when the app has
    # "Require App Secret" switched on; it is harmless when it doesn't.
    if settings.META_APP_SECRET:
        params["appsecret_proof"] = hmac.new(
            settings.META_APP_SECRET.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    return params


def _parse(response):
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error")
    if response.ok and not error:
        return body
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or f"HTTP {response.status_code}")
    if error.get("code") in TOKEN_ERROR_CODES:
        raise MetaError("error.metaTokenInvalid", message)
    raise MetaError("error.metaRequestFailed", message)


def graph_get(path, token, params=None):
    try:
        response = requests.get(
            _url(path), params={**(params or {}), **_auth_params(token)}, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException:
        logger.exception("Meta Graph API GET %s failed", path)
        raise MetaError("error.metaUnreachable") from None
    return _parse(response)


def graph_post(path, token, data, timeout=REQUEST_TIMEOUT_SECONDS):
    """
    A Graph call that creates something. Unlike a GET, a POST that times out
    may still have worked on Meta's side, so callers must not treat
    error.metaUnreachable as "nothing happened".
    """
    try:
        response = requests.post(_url(path), data={**data, **_auth_params(token)}, timeout=timeout)
    except requests.RequestException:
        logger.exception("Meta Graph API POST %s failed", path)
        raise MetaError("error.metaUnreachable") from None
    return _parse(response)


# What "Connect with Facebook" asks the owner to grant: list and post to
# their Pages, and publish to the Instagram account linked to one.
# business_management is needed for /me/accounts to include Pages owned
# through a Business portfolio. Each needs App Review for other people's
# Pages (see docs/social-campaigns-plan.md, Meta prerequisites).
OAUTH_SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
)
# Page roles that can publish (/me/accounts `tasks`).
POSTING_TASKS = {"CREATE_CONTENT", "MANAGE"}


def oauth_dialog_url(state):
    """The Facebook Login dialog the owner's browser is sent to."""
    query = urlencode({
        "client_id": settings.META_APP_ID,
        "redirect_uri": settings.META_OAUTH_REDIRECT_URI,
        "state": state,
        "scope": ",".join(OAUTH_SCOPES),
        "response_type": "code",
    })
    return f"https://www.facebook.com/{settings.META_GRAPH_API_VERSION}/dialog/oauth?{query}"


def _oauth_token(params):
    """
    One call to /oauth/access_token, which is authenticated by the app
    secret rather than a token, so it doesn't go through graph_get.
    """
    try:
        response = requests.get(
            _url("oauth/access_token"),
            params={"client_id": settings.META_APP_ID, "client_secret": settings.META_APP_SECRET, **params},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.exception("Meta OAuth token exchange failed")
        raise MetaError("error.metaUnreachable") from None
    try:
        body = _parse(response)
    except MetaError as error:
        # An expired or already-used code, or a redirect URI that doesn't
        # match the one registered: either way, logging in again is the fix.
        raise MetaError("error.metaOauthFailed", error.detail) from None
    token = body.get("access_token") or ""
    if not token:
        raise MetaError("error.metaOauthFailed")
    return token


def exchange_code(code):
    """
    The login code for a long-lived (about 60 days) user token. Page tokens
    read with a long-lived user token don't expire, which is the reason for
    the second step: a short-lived one would leave the Page connection
    dying within the hour.
    """
    short = _oauth_token({"redirect_uri": settings.META_OAUTH_REDIRECT_URI, "code": code})
    return _oauth_token({"grant_type": "fb_exchange_token", "fb_exchange_token": short})


def list_pages(user_token):
    """The Pages the user manages, with their Instagram accounts. No tokens."""
    body = graph_get(
        "me/accounts", user_token,
        {"fields": "id,name,tasks,instagram_business_account{id,username}", "limit": 100},
    )
    pages = []
    for page in body.get("data") or []:
        if not isinstance(page, dict) or not page.get("id"):
            continue
        instagram = page.get("instagram_business_account") or {}
        pages.append({
            "id": str(page["id"]),
            "name": page.get("name") or "",
            "ig_user_id": str(instagram.get("id") or ""),
            "ig_username": instagram.get("username") or "",
            # A Page the user can only moderate or analyse can't be posted
            # to; shown, but not offered.
            "can_post": bool(POSTING_TASKS & set(page.get("tasks") or [])),
        })
    return pages


def resolve_page(page_id, token):
    """
    Work out the Page, its own access token and its Instagram account from
    whatever token the owner pasted.

    Two kinds of token are accepted, because Meta's tools hand out both and
    telling them apart is not something a restaurant owner should have to do:

    - A Page token: /me *is* the Page, so the token is used as-is.
    - A User token belonging to one of the Page's admins: the Page's own
      token is read from the Page. A long-lived user token yields a Page
      token that doesn't expire, which is what publishing needs.
    """
    me = graph_get("me", token, {"fields": "id"})
    if str(me.get("id")) == page_id:
        page = graph_get(page_id, token, {"fields": "id,name,instagram_business_account"})
        page_token = token
    else:
        page = graph_get(page_id, token, {"fields": "id,name,access_token,instagram_business_account"})
        page_token = page.get("access_token") or ""
        if not page_token:
            # The user can see the Page but isn't one of its admins, or the
            # token lacks the Page permissions. Either way it can't post.
            raise MetaError("error.metaNotPageAdmin")

    ig_user_id = str((page.get("instagram_business_account") or {}).get("id") or "")
    ig_username = ""
    if ig_user_id:
        try:
            ig_username = graph_get(ig_user_id, page_token, {"fields": "username"}).get("username") or ""
        except MetaError as error:
            # Only the display name is lost; the account id is what posting
            # uses. Usually means instagram_basic wasn't granted, which the
            # first Instagram post will report properly.
            if error.token_invalid:
                raise
            logger.warning("Could not read Instagram username for %s: %s", ig_user_id, error.detail)

    return PageInfo(
        page_id=str(page.get("id") or page_id),
        page_name=page.get("name") or "",
        page_token=page_token,
        ig_user_id=ig_user_id,
        ig_username=ig_username,
    )
