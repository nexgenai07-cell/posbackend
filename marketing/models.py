from django.db import models

from accounts.models import Staff
from branches.models import Branch
from common.models import BaseModel
from customers.models import Customer, MarketingConsent


class MediaType(models.TextChoices):
    IMAGE = "image", "Image"
    VIDEO = "video", "Video"


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

    # Where the campaign goes. Separate columns rather than a list, for the
    # same reason the audience rule is: queryable, and a typo is an error.
    # WhatsApp is a message per customer (the recipients below); Facebook
    # and Instagram are one public post each, with no audience at all.
    send_whatsapp = models.BooleanField(default=True)
    post_facebook = models.BooleanField(default=False)
    post_instagram = models.BooleanField(default=False)
    # The public post's text. Not message_en/message_ar: those are written to
    # one customer and may say {{name}}, which means nothing on a Page. One
    # field, in whatever language(s) the restaurant writes it. 2200 is
    # Instagram's caption limit.
    post_caption = models.TextField(blank=True, max_length=2200)
    # One image or video, uploaded to the restaurant's own Cloudinary
    # account (common/cloudinary.py) — Instagram fetches media from a public
    # URL rather than taking an upload, so it has to live somewhere public.
    media_type = models.CharField(max_length=5, choices=MediaType.choices, blank=True)
    media_url = models.CharField(max_length=500, blank=True)

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
        A campaign stops being editable once its recipients are frozen, or
        once anything has gone up on a Page. Editing the message after that
        would make the send log a record of words nobody received — the same
        snapshot-immutability rule order totals follow.
        """
        return self.status == CampaignStatus.DRAFT and not self.has_live_posts

    @property
    def has_live_posts(self):
        """Whether a post is up, or on its way up, on any channel."""
        return self.posts.filter(status__in=LIVE_POST_STATUSES).exists()


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


class PostChannel(models.TextChoices):
    FACEBOOK = "facebook", "Facebook"
    INSTAGRAM = "instagram", "Instagram"


class PostStatus(models.TextChoices):
    PENDING = "pending", "Not posted yet"
    # Instagram only: the video was accepted and Meta is still processing
    # it. Publishing finishes when it is ready (M4).
    PROCESSING = "processing", "Processing"
    POSTED = "posted", "Posted"
    FAILED = "failed", "Failed"


# A post in either state is (or is about to be) public. Publishing skips
# it, and the campaign's wording can no longer change.
LIVE_POST_STATUSES = (PostStatus.POSTED, PostStatus.PROCESSING)


class CampaignPost(BaseModel):
    """
    One campaign on one public channel — the social counterpart of
    CampaignRecipient.

    Caption and media are snapshots taken when the post was attempted, for
    the same reason a recipient's message is: this is the record of what
    went up on the Page, whatever happens to the campaign afterwards.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="posts")
    channel = models.CharField(max_length=10, choices=PostChannel.choices)
    status = models.CharField(max_length=10, choices=PostStatus.choices, default=PostStatus.PENDING)
    # Instagram's media container while a video is processing (M4).
    container_id = models.CharField(max_length=64, blank=True)
    # The live post's id on Meta and its public link. The link is looked up
    # after posting; a post can be live with no link if that lookup failed.
    external_id = models.CharField(max_length=128, blank=True)
    permalink = models.CharField(max_length=500, blank=True)
    caption = models.TextField(blank=True, max_length=2200)
    media_url = models.CharField(max_length=500, blank=True)
    # An error code (error.metaTokenInvalid, ...) or Meta's own short message.
    error = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["channel", "id"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "channel"], name="one_post_per_channel"),
        ]

    def __str__(self):
        return f"{self.campaign_id} {self.channel} ({self.status})"


class SocialProvider(models.TextChoices):
    # One Meta connection covers both Facebook and Instagram: Instagram
    # content is published with the linked Facebook Page's token, so they
    # can't be connected or expire independently. WhatsApp joins this list
    # when its Cloud API moves from env to per-restaurant settings.
    META = "meta", "Facebook & Instagram"


class ConnectionStatus(models.TextChoices):
    CONNECTED = "connected", "Connected"
    # Meta rejected the token itself (revoked, password changed, expired).
    # Only reconnecting with a new token fixes it.
    EXPIRED = "expired", "Needs reconnecting"
    # Meta answered with some other problem the last time it was checked.
    ERROR = "error", "Error"


class SocialConnection(models.Model):
    """
    One restaurant's link to a social account, entered by its owner.

    Attached to Branch because Branch is the restaurant in this project (it
    already carries the branding, and ZatcaSettings hangs off it the same
    way). Code reads it only through marketing.social.get_social_connection(),
    so a Restaurant model above Branch later moves this one table and that
    one function.

    Not a BaseModel: disconnecting deletes the row for real. A soft-deleted
    row would keep an encrypted token in the database that nobody can see or
    use, which is exactly the thing disconnecting is meant to get rid of.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="social_connections")
    provider = models.CharField(max_length=16, choices=SocialProvider.choices)
    status = models.CharField(max_length=10, choices=ConnectionStatus.choices, default=ConnectionStatus.CONNECTED)

    page_id = models.CharField(max_length=64)
    page_name = models.CharField(max_length=255, blank=True)
    # The Instagram professional account linked to the Page, if any. Blank
    # means Facebook works and Instagram is unavailable until one is linked
    # on Meta's side and the connection is checked again.
    ig_user_id = models.CharField(max_length=64, blank=True)
    ig_username = models.CharField(max_length=255, blank=True)

    # Fernet ciphertext (marketing.social), never the token itself. Nothing
    # serializes this field; the admin only ever learns that a token is set.
    access_token_enc = models.TextField(blank=True)

    # An error code (error.metaTokenInvalid, ...) or Meta's own short message
    # from the last failed check. Blank while everything works.
    last_error = models.CharField(max_length=255, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="social_connections"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "id"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "provider"], name="one_social_connection_per_provider"),
        ]

    def __str__(self):
        return f"{self.get_provider_display()}: {self.page_name or self.page_id} ({self.status})"


class CampaignAiDraft(models.Model):
    """
    One "Write with AI" request on the campaign form. Kept so a branch's
    daily allowance can be enforced (every request costs money, even one
    that fails) and spend looked up later — the same job imports.AiDraft
    does for "Fill with AI", counted separately so writing captions can't
    use up the menu import's allowance or the other way round.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="campaign_ai_drafts")
    requested_by = models.ForeignKey(Staff, on_delete=models.SET_NULL, null=True, related_name="campaign_ai_drafts")
    created_at = models.DateTimeField(auto_now_add=True)
    succeeded = models.BooleanField(default=False)
    error = models.CharField(max_length=64, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["branch", "created_at"])]


__all__ = [
    "AudienceConsent",
    "Campaign",
    "CampaignAiDraft",
    "CampaignPost",
    "CampaignRecipient",
    "CampaignStatus",
    "ConnectionStatus",
    "MarketingConsent",
    "LIVE_POST_STATUSES",
    "MediaType",
    "PostChannel",
    "PostStatus",
    "RecipientStatus",
    "SocialConnection",
    "SocialProvider",
]
