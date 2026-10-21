"""
Audience resolution and message rendering for campaigns.

The split that matters here: an audience is a *rule* while a campaign is a
draft, and a frozen list of CampaignRecipient rows once it is prepared.
Previewing re-runs the rule; sending never does. Without that, a customer who
ordered (or opted out) halfway through a send would change the list underneath
it, and the send log would stop being a record of what actually happened.
"""

from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.db.models import Count, Max, Q
from django.utils import timezone

from customers.models import Customer, MarketingConsent
from customers.services import normalize_phone
from orders.models import OrderStatus

from .models import AudienceConsent, CampaignRecipient, RecipientStatus

# A preview never needs the whole list - it answers "how many, and does the
# message look right on a real row".
AUDIENCE_SAMPLE_SIZE = 10

# Guards a single campaign against being pointed at the entire customer table
# by accident. A real restaurant campaign is hundreds, not tens of thousands,
# and on the manual path every recipient is a link somebody has to tap.
MAX_RECIPIENTS = 5000

# Shortest national number that could be a real phone. Saudi mobiles are 9
# digits after the country code and Pakistani ones 10, while landlines run to
# 7-8, so 8 is the floor that still admits every real number. Below this a row
# is junk data - a till operator typing "787878" into the phone box to get
# past it - and turning that into a wa.me link would produce a chat with a
# stranger, or with nobody.
MIN_NATIONAL_DIGITS = 8

CONSENT_STATES = {
    AudienceConsent.OPTED_IN: [MarketingConsent.OPTED_IN],
    AudienceConsent.NOT_OPTED_OUT: [MarketingConsent.OPTED_IN, MarketingConsent.UNKNOWN],
}


def audience_queryset(branch, *, consent, min_orders, ordered_within_days, not_ordered_for_days, now=None):
    """
    Customers matching an audience rule, annotated with `order_count` and
    `last_order`.

    Both figures are annotated from Order rather than read off
    Customer.last_order_at: that column is maintained by one code path
    (orders' OrderCustomerView) and would quietly disagree with order_count
    for any row it missed. Counting both from the same query means the
    audience can never contain a customer whose two numbers contradict.
    """
    now = now or timezone.now()
    not_cancelled = ~Q(orders__status=OrderStatus.CANCELLED)

    queryset = (
        Customer.objects.filter(branch=branch)
        .annotate(
            order_count=Count("orders", filter=not_cancelled, distinct=True),
            last_order=Max("orders__opened_at", filter=not_cancelled),
        )
        .filter(marketing_consent__in=CONSENT_STATES[consent])
    )

    if min_orders:
        queryset = queryset.filter(order_count__gte=min_orders)
    if ordered_within_days:
        queryset = queryset.filter(last_order__gte=now - timedelta(days=ordered_within_days))
    if not_ordered_for_days:
        # Strictly "nothing since": a customer with no orders at all has
        # last_order = NULL and is excluded, because "hasn't ordered in 60
        # days" is a win-back rule about people who used to come, not about
        # people who never did.
        queryset = queryset.filter(last_order__lte=now - timedelta(days=not_ordered_for_days))

    return queryset.order_by("-last_order", "pk")


def campaign_audience(campaign, now=None):
    """The audience_queryset() for a campaign's own saved rule."""
    return audience_queryset(
        campaign.branch,
        consent=campaign.audience_consent,
        min_orders=campaign.min_orders,
        ordered_within_days=campaign.ordered_within_days,
        not_ordered_for_days=campaign.not_ordered_for_days,
        now=now,
    )


# -- message rendering -------------------------------------------------------


def sendable_customers(queryset, limit=MAX_RECIPIENTS):
    """
    The audience members a message can actually reach, in order, capped.

    Phone usability is decided in Python (to_international normalises Arabic
    digits, strips punctuation and applies the country code), so it cannot be
    pushed into the SQL filter. Both the preview and the real prepare step go
    through here rather than each applying their own rule - otherwise the
    preview would promise a number that prepare then quietly failed to match,
    which is exactly the kind of small lie that makes staff stop trusting the
    screen.

    Cost: this walks up to `limit` rows instead of running a COUNT. At
    restaurant scale (thousands of customers, debounced previews) that is
    cheap; if the customer table ever reaches a size where it is not, the fix
    is a stored normalised-phone column, not a second definition of "valid".
    """
    return [customer for customer in queryset[:limit] if to_international(customer.phone)]


def message_template(campaign):
    """
    Arabic when the campaign has it, English otherwise.

    There is no per-customer locale to branch on - the QR flow collects a
    phone and nothing else - so this is a per-campaign choice, and Arabic
    wins because the guests read it first.
    """
    return campaign.message_ar.strip() or campaign.message_en.strip()


def render_message(template, *, customer_name="", phone="", restaurant="", fallback_name=""):
    """
    Fill {{name}}, {{phone}} and {{restaurant}} in a campaign message.

    An unknown name is the normal case (most customers only ever gave a
    phone), so a blank one falls back to the campaign's `fallback_name` and,
    failing that, is removed along with the whitespace and dangling comma it
    would otherwise leave behind - "Hi ," reads worse than "Hi".
    """
    name = (customer_name or "").strip() or (fallback_name or "").strip()
    text = template or ""
    for token, value in (("name", name), ("phone", phone or ""), ("restaurant", restaurant or "")):
        text = text.replace("{{" + token + "}}", value)
    return _tidy(text)


# Punctuation that should close up against the word before it once an empty
# placeholder has left a gap. The last entry is the Arabic comma (U+060C).
_DANGLING = (" ,", " .", " !", " ?", " ،")


def _tidy(text):
    """Collapses the gaps an empty placeholder leaves, line by line."""
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        for dangling in _DANGLING:
            line = line.replace(dangling, dangling[1])
        lines.append(line.strip())
    return "\n".join(lines).strip()


# Between the message and the media link: a blank line, so WhatsApp shows
# the link (and its preview card) apart from the text.
MEDIA_LINK_SEPARATOR = "\n\n"


def with_media_link(text, media_url):
    """
    The message with the campaign's image or video link after it.

    A wa.me link can only carry text, so this is how a WhatsApp campaign
    shows its media: WhatsApp turns the link into a preview. Added after
    placeholders are filled, so a {{name}} can never end up inside the URL.
    """
    if not media_url:
        return text
    return f"{text}{MEDIA_LINK_SEPARATOR}{media_url}" if text else media_url


def media_link_length(media_url):
    """How many characters with_media_link() adds to a message."""
    return len(MEDIA_LINK_SEPARATOR) + len(media_url) if media_url else 0


def preview_message(campaign, customer=None):
    """The campaign's message as one real customer would receive it."""
    text = render_message(
        message_template(campaign),
        customer_name=getattr(customer, "name", "") or "",
        phone=getattr(customer, "phone", "") or "",
        restaurant=campaign.branch.name_ar or campaign.branch.name_en,
        fallback_name=campaign.fallback_name,
    )
    return with_media_link(text, campaign.media_url)


# -- phone numbers -----------------------------------------------------------


def to_international(phone, country_code=None):
    """
    A wa.me-ready number: digits only, country code included, no "+".

    Stored numbers are whatever the customer typed - "0501234567",
    "+966 50 123 4567", "00966501234567" all mean the same person
    (customers/services.py already normalises them for matching). wa.me needs
    the full international form, so a national number gets the branch's
    country code prefixed.

    Returns "" for anything too short to be a real number, so the caller can
    leave that customer out rather than build a link to nobody.

    Limitation worth knowing: a number stored with no country code is assumed
    to be local. A Pakistani mobile saved as "03001234567" in a Saudi branch
    would get 966 in front of it and the link would be wrong - which is why
    the admin shows the resolved number next to every link instead of only
    the original, so staff can see it before they tap.
    """
    code = str(country_code or settings.MARKETING_DEFAULT_COUNTRY_CODE).strip()
    digits = normalize_phone(phone)
    if not digits:
        return ""
    if code and digits.startswith(code) and len(digits) > len(code):
        return digits if len(digits) - len(code) >= MIN_NATIONAL_DIGITS else ""
    national = digits.lstrip("0")
    return code + national if len(national) >= MIN_NATIONAL_DIGITS else ""


def whatsapp_link(phone, message, country_code=None):
    """
    A click-to-chat link that opens WhatsApp with the message pre-typed.

    This is the free path: nothing is sent by us, the staff member taps and
    WhatsApp sends it from the restaurant's own number. No Meta Business
    verification, no template approval, no per-message cost.
    """
    number = to_international(phone, country_code)
    if not number:
        return ""
    return "https://wa.me/" + number + "?text=" + quote(message or "")


# -- preparing ---------------------------------------------------------------


def build_recipients(campaign, now=None):
    """
    Freeze the campaign's audience into CampaignRecipient rows.

    Returns the number created. Customers with an unusable phone are left out
    rather than stored as a recipient that can never be sent - they would only
    ever sit in the list as a permanent failure.
    """
    now = now or timezone.now()
    restaurant = campaign.branch.name_ar or campaign.branch.name_en
    template = message_template(campaign)

    existing = set(campaign.recipients.values_list("customer_id", flat=True))
    rows = []
    for customer in sendable_customers(campaign_audience(campaign, now=now)):
        if customer.pk in existing:
            continue
        rows.append(CampaignRecipient(
            campaign=campaign,
            customer=customer,
            phone=customer.phone,
            name=customer.name or "",
            message=with_media_link(
                render_message(
                    template,
                    customer_name=customer.name,
                    phone=customer.phone,
                    restaurant=restaurant,
                    fallback_name=campaign.fallback_name,
                ),
                campaign.media_url,
            ),
            status=RecipientStatus.PENDING,
        ))

    CampaignRecipient.objects.bulk_create(rows)
    return len(rows)


def recipient_counts(campaign):
    """Per-status counts for the campaign list and detail header."""
    rows = campaign.recipients.values("status").annotate(count=Count("id"))
    counts = {value: 0 for value in RecipientStatus.values}
    counts.update({row["status"]: row["count"] for row in rows})
    counts["total"] = sum(counts[value] for value in RecipientStatus.values)
    return counts
