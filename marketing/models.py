from django.db import models

from accounts.models import Staff
from branches.models import Branch
from common.models import BaseModel
from customers.models import Customer, MarketingConsent


class CampaignChannel(models.TextChoices):
    # Only WhatsApp for now. The field exists so adding SMS/email later is a
    # row value rather than a schema change — the same reason Order.source
    # carried `qr` before the QR flow was built.
    WHATSAPP = "whatsapp", "WhatsApp"


class CampaignStatus(models.TextChoices):
    # Being written. The audience is still a *rule*, re-evaluated on every
    # preview, and no recipient rows exist yet.
    DRAFT = "draft", "Draft"
    # The rule has been resolved into a frozen recipient list. Everything
    # after this point works off that snapshot, so a customer ordering (or
    # opting out) mid-send can't change who is already queued.
    READY = "ready", "Ready to send"
    # At least one recipient has gone out, not all of them have.
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    CANCELLED = "cancelled", "Cancelled"


class AudienceConsent(models.TextChoices):
    """
    Which consent states a campaign is willing to message.

    OPTED_IN is the only setting WhatsApp's Business Policy allows for a
    marketing template sent over the Cloud API. NOT_OPTED_OUT also includes
    customers who were never asked — defensible for a manual send from the
    owner's own phone to people who handed over their number to order, and
    deliberately *not* defensible for automated template sending, which is
    why senders.py refuses it (see CloudApiSender.check_audience).
    """

    OPTED_IN = "opted_in", "Opted in only"
    NOT_OPTED_OUT = "not_opted_out", "Everyone except opt-outs"


class Campaign(BaseModel):
    """
    One marketing send: a message, a rule describing who gets it, and — once
    prepared — the frozen list of who it actually went to.

    The audience is stored as explicit columns rather than a JSON blob so it
    stays queryable and a typo is a migration error instead of a silently
    ignored key. Four knobs cover what the restaurant actually asks for:
    "my regulars" (min_orders), "people who came recently" (ordered_within_
    days), "people who have stopped coming" (not_ordered_for_days), and who
    has agreed to be messaged (audience_consent).
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="campaigns")
    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=12, choices=CampaignChannel.choices, default=CampaignChannel.WHATSAPP)
    status = models.CharField(max_length=10, choices=CampaignStatus.choices, default=CampaignStatus.DRAFT)

    # Both languages are optional individually, but at least one must be set
    # (CampaignSerializer.validate). Each customer gets the Arabic text when
    # it exists, falling back to English — we have no per-customer locale to
    # go on, and the restaurant's own guests read Arabic first.
    message_en = models.TextField(blank=True, max_length=1000)
    message_ar = models.TextField(blank=True, max_length=1000)
    # Substituted into {{name}} when the customer's name is blank, which is
    # most of them — the QR flow only ever requires a phone.
    fallback_name = models.CharField(max_length=60, blank=True)

    audience_consent = models.CharField(
        max_length=14, choices=AudienceConsent.choices, default=AudienceConsent.NOT_OPTED_OUT
    )
    # 0 = everyone on file, 1 = has ordered at least once, 2 = repeat
    # customers only, and so on. Counts non-cancelled orders, matching
    # the repeat-customers report.
    min_orders = models.PositiveSmallIntegerField(default=1)
    # "Ordered in the last N days" / "hasn't ordered for N days". Both null
    # means no recency filter; both set is a valid window (e.g. ordered in
    # the last 180 days but not in the last 30 = lapsing regulars).
    ordered_within_days = models.PositiveSmallIntegerField(null=True, blank=True)
    not_ordered_for_days = models.PositiveSmallIntegerField(null=True, blank=True)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    prepared_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns"
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["branch", "status", "-created_at"])]

    def __str__(self):
        return f"{self.name} ({self.status})"

    @property
    def is_editable(self):
        """
        A campaign stops being editable once its recipients are frozen.
        Editing the message after that would make the send log a record of
        words nobody received — the same snapshot-immutability rule order
        totals follow.
        """
        return self.status == CampaignStatus.DRAFT


class RecipientStatus(models.TextChoices):
    PENDING = "pending", "Not sent yet"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    # Excluded at send time rather than at prepare time — e.g. the customer
    # opted out between preparing and sending.
    SKIPPED = "skipped", "Skipped"


class CampaignRecipient(BaseModel):
    """
    One person on one campaign, with the message they were actually sent.

    Phone, name and message are snapshots, not lookups: this is the record
    of what went out, and it has to stay true after the customer renames
    themselves, changes number or is deleted (hence SET_NULL on `customer`).
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaign_recipients"
    )
    phone = models.CharField(max_length=32)
    name = models.CharField(max_length=255, blank=True)
    # The rendered text for this one person, placeholders already filled in.
    message = models.TextField(max_length=1000)
    status = models.CharField(max_length=8, choices=RecipientStatus.choices, default=RecipientStatus.PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    # An error *code* (error.whatsappNotConfigured, ...) or the provider's own
    # message. Shown to staff as-is, so it stays short.
    error = models.CharField(max_length=255, blank=True)
    # The provider's id for the sent message, for matching up delivery
    # webhooks later. Empty for a manual send — there is nothing to match.
    external_id = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "customer"], name="unique_customer_per_campaign"),
        ]
        indexes = [models.Index(fields=["campaign", "status"])]

    def __str__(self):
        return f"{self.phone} ({self.status})"


__all__ = [
    "AudienceConsent",
    "Campaign",
    "CampaignChannel",
    "CampaignRecipient",
    "CampaignStatus",
    "MarketingConsent",
    "RecipientStatus",
]
