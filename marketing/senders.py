"""
How a campaign message actually leaves the building.

Two paths, one interface, because they have very different prerequisites:

- ManualSender is the default and needs nothing. The server renders each
  message and a wa.me link; a staff member taps it and WhatsApp sends from
  the restaurant's own number. Free, instant, no Meta approvals - and it
  cannot be automated, which is the whole point: nothing goes out that a
  human did not press send on.

- CloudApiSender talks to Meta's WhatsApp Cloud API. It only exists once
  WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN are configured, which
  requires Meta Business verification and pre-approved message templates.
  Until then `cloud_api_sender()` returns None and the send endpoint answers
  error.whatsappNotConfigured rather than pretending.

Campaigns, audiences and the admin UI are written against this seam, so
turning the Cloud API on later is configuration plus a template id - not a
rewrite of anything above.
"""

import logging

import requests
from django.conf import settings

from .models import AudienceConsent

logger = logging.getLogger(__name__)

# Meta rejects a request that hangs around; a campaign send is a loop over
# many of these, so a slow number must not stall the whole run.
REQUEST_TIMEOUT_SECONDS = 15


class SendResult:
    """Outcome of one message. `error` is an error code or a provider string."""

    __slots__ = ("ok", "external_id", "error")

    def __init__(self, ok, external_id="", error=""):
        self.ok = ok
        self.external_id = external_id
        self.error = error


class SenderError(Exception):
    """Raised for a problem with the whole send, not one recipient."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BaseSender:
    #: Shown to the admin so the campaign page can explain what will happen.
    key = ""

    def check_audience(self, campaign):
        """Raise SenderError if this audience may not be sent on this channel."""

    def send(self, recipient):  # pragma: no cover - interface only
        raise NotImplementedError


class ManualSender(BaseSender):
    """
    The free path. Never sends anything itself.

    Calling send() is a programming error rather than a runtime one: the
    views route manual campaigns to "mark as sent" after a human has tapped
    the link, and never here.
    """

    key = "manual"

    def send(self, recipient):
        raise SenderError("error.manualSendOnly")


class CloudApiSender(BaseSender):
    """
    Meta WhatsApp Cloud API. Costs per message and needs approved templates.

    Only `template` messages may be sent to someone who has not messaged the
    business in the last 24 hours, which is every recipient of a marketing
    campaign - so this always sends a template, never free-form text. The
    campaign's own message body is passed as the template's first body
    parameter, which means the approved template must be a single
    {{1}} placeholder.
    """

    key = "cloud_api"

    def __init__(self, phone_number_id, access_token, template_name, template_language, api_version):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.template_name = template_name
        self.template_language = template_language
        self.api_version = api_version

    @property
    def endpoint(self):
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

    def check_audience(self, campaign):
        """
        Meta's Business Policy requires prior opt-in for marketing templates.

        NOT_OPTED_OUT includes customers who were never asked, which is fine
        for a human sending from their own phone and not fine here - sending
        it anyway risks the number's quality rating and, eventually, the
        account. Blocked rather than warned about.
        """
        if campaign.audience_consent != AudienceConsent.OPTED_IN:
            raise SenderError("error.consentRequiredForApiSend")

    def send(self, recipient):
        from .services import to_international

        number = to_international(recipient.phone)
        if not number:
            return SendResult(False, error="error.invalidPhone")

        payload = {
            "messaging_product": "whatsapp",
            "to": number,
            "type": "template",
            "template": {
                "name": self.template_name,
                "language": {"code": self.template_language},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": recipient.message}]}
                ],
            },
        }
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            # Network-level failure: the recipient stays failed and can be
            # retried, because nothing confirms the message was accepted.
            logger.exception("WhatsApp Cloud API request failed for recipient %s", recipient.pk)
            return SendResult(False, error="error.whatsappUnreachable")

        body = _json_or_empty(response)
        if not response.ok:
            # Meta's own message is more useful to staff than a generic code
            # ("Template name does not exist in en", a 24h-window rejection,
            # a spending-limit block). Truncated to the column width.
            message = (body.get("error") or {}).get("message") or f"http.{response.status_code}"
            return SendResult(False, error=str(message)[:255])

        messages = body.get("messages") or [{}]
        return SendResult(True, external_id=str(messages[0].get("id") or ""))


def _json_or_empty(response):
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def whatsapp_cloud_configured():
    """Whether automated sending is available at all."""
    return bool(settings.WHATSAPP_PHONE_NUMBER_ID and settings.WHATSAPP_ACCESS_TOKEN)


def cloud_api_sender():
    """The configured Cloud API sender, or None when it is not set up."""
    if not whatsapp_cloud_configured():
        return None
    return CloudApiSender(
        phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID,
        access_token=settings.WHATSAPP_ACCESS_TOKEN,
        template_name=settings.WHATSAPP_TEMPLATE_NAME,
        template_language=settings.WHATSAPP_TEMPLATE_LANGUAGE,
        api_version=settings.WHATSAPP_API_VERSION,
    )


def manual_sender():
    return ManualSender()
