import re

from rest_framework import serializers

from common import cloudinary

from .models import AudienceConsent, Campaign, CampaignPost, CampaignRecipient, SocialConnection
from .services import media_link_length, whatsapp_link

# The recipient message's cap (CampaignRecipient.message). The media link is
# appended to the WhatsApp text, so it has to fit inside this too.
MAX_MESSAGE_LENGTH = 1000

# Longest sensible recency window. Two years of "hasn't ordered since" is
# already a dead customer; the cap exists so a typo'd 9999 can't quietly
# produce an empty audience that looks like a bug.
MAX_RECENCY_DAYS = 730

# Instagram rejects a caption with more than 30 hashtags outright.
MAX_INSTAGRAM_HASHTAGS = 30
HASHTAG = re.compile(r"#\w+")


class AudienceRuleSerializer(serializers.Serializer):
    """
    The audience half of a campaign, on its own.

    Used by the preview endpoint so the admin can count an audience before
    there is a campaign to save it on - the first question anyone asks is
    "how many people is that", and making them save a draft to find out
    would be the wrong order.
    """

    audience_consent = serializers.ChoiceField(
        choices=AudienceConsent.choices, default=AudienceConsent.NOT_OPTED_OUT
    )
    min_orders = serializers.IntegerField(min_value=0, max_value=1000, default=1)
    ordered_within_days = serializers.IntegerField(
        min_value=1, max_value=MAX_RECENCY_DAYS, required=False, allow_null=True, default=None
    )
    not_ordered_for_days = serializers.IntegerField(
        min_value=1, max_value=MAX_RECENCY_DAYS, required=False, allow_null=True, default=None
    )

    def validate(self, attrs):
        within = attrs.get("ordered_within_days")
        since = attrs.get("not_ordered_for_days")
        # "Ordered in the last 7 days but not for 30" can never match anyone.
        # A window only makes sense the other way round: ordered within a
        # long period, but not within a shorter recent one.
        if within and since and since >= within:
            raise serializers.ValidationError({"not_ordered_for_days": "error.impossibleAudienceWindow"})
        return attrs


class CampaignPreviewSerializer(AudienceRuleSerializer):
    """
    An unsaved campaign form: the audience rule plus whatever has been typed
    into the message so far, so one request answers both "who gets this" and
    "what will it look like" while the form is still open.
    """

    message_en = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)
    message_ar = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)
    fallback_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=60)
    # Only so the preview shows the link the real message will end with.
    # Nothing is posted from here, so it isn't checked against Cloudinary.
    media_url = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)


class CampaignSerializer(serializers.ModelSerializer):
    # error_messages, not just validate_name(): a blank value fails at the
    # field level before any validate_*() runs, so without this a missing name
    # answers with DRF's default English sentence instead of an error code the
    # admin can translate.
    name = serializers.CharField(
        max_length=120,
        error_messages={
            "blank": "error.campaignNameRequired",
            "required": "error.campaignNameRequired",
            "null": "error.campaignNameRequired",
        },
    )
    recipient_counts = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Campaign
        fields = [
            "id", "name", "status",
            "send_whatsapp", "post_facebook", "post_instagram",
            "message_en", "message_ar", "fallback_name",
            "post_caption", "media_type", "media_url",
            "audience_consent", "min_orders", "ordered_within_days", "not_ordered_for_days",
            "scheduled_for", "prepared_at", "sent_at",
            "recipient_counts", "is_editable", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "status", "prepared_at", "sent_at",
            "recipient_counts", "is_editable", "created_by_name", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "media_type": {"error_messages": {"invalid_choice": "error.campaignMediaInvalid"}},
            "post_caption": {"error_messages": {"max_length": "error.captionTooLong"}},
        }

    def get_recipient_counts(self, campaign):
        # Annotated by the viewset's queryset for lists; falls back to a
        # per-row query for a single retrieve, where one extra query is fine.
        from .services import recipient_counts

        return recipient_counts(campaign)

    def get_created_by_name(self, campaign):
        return campaign.created_by.name if campaign.created_by_id else None

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("error.campaignNameRequired")
        return value

    def validate_message_en(self, value):
        return value.strip()

    def validate_message_ar(self, value):
        return value.strip()

    def validate_post_caption(self, value):
        return value.strip()

    def validate(self, attrs):
        # Fall back to the saved value on a PATCH that only touches one field.
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        whatsapp, facebook, instagram = current("send_whatsapp"), current("post_facebook"), current("post_instagram")
        if whatsapp is None:
            # Created without saying: the model default, so a client written
            # before channels existed still makes a WhatsApp campaign.
            whatsapp = True
        if not (whatsapp or facebook or instagram):
            raise serializers.ValidationError({"send_whatsapp": "error.campaignChannelRequired"})

        # The WhatsApp message is only needed when WhatsApp is ticked; a
        # Facebook-only campaign has nobody to address it to.
        if whatsapp and not (current("message_en") or current("message_ar")):
            raise serializers.ValidationError({"message_en": "error.campaignMessageRequired"})

        media_type, media_url = current("media_type") or "", current("media_url") or ""
        if bool(media_type) != bool(media_url):
            raise serializers.ValidationError({"media_url": "error.campaignMediaInvalid"})
        if media_url and not cloudinary.is_own_upload(media_url, media_type):
            # Only files uploaded through this admin: whatever is here gets
            # posted publicly under the restaurant's name.
            raise serializers.ValidationError({"media_url": "error.campaignMediaInvalid"})

        if whatsapp and media_url:
            # The link goes at the end of the WhatsApp text (M5), so it has
            # to fit. Placeholders can still lengthen a message; a name is
            # short next to a URL, and render_message has always had that.
            for field in ("message_en", "message_ar"):
                if len(current(field) or "") + media_link_length(media_url) > MAX_MESSAGE_LENGTH:
                    raise serializers.ValidationError({field: "error.messageTooLongForMedia"})

        caption = current("post_caption") or ""
        if facebook and not (caption or media_url):
            raise serializers.ValidationError({"post_caption": "error.facebookPostEmpty"})
        if instagram and not media_url:
            # Instagram has no text-only posts.
            raise serializers.ValidationError({"media_url": "error.instagramNeedsMedia"})
        if instagram and len(HASHTAG.findall(caption)) > MAX_INSTAGRAM_HASHTAGS:
            raise serializers.ValidationError({"post_caption": "error.instagramTooManyHashtags"})

        within, since = current("ordered_within_days"), current("not_ordered_for_days")
        if within and since and since >= within:
            raise serializers.ValidationError({"not_ordered_for_days": "error.impossibleAudienceWindow"})
        return attrs


class CampaignAiDraftSerializer(serializers.Serializer):
    """
    What "Write with AI" works from: a product (its deal comes with it, a
    deal being one per product), free notes, and which texts to write. Works
    for an unsaved form, so it takes the form's channel ticks, not a campaign.
    """

    product_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000,
        error_messages={"max_length": "imports.aiTextTooLong"},
    )
    send_whatsapp = serializers.BooleanField(default=True)
    post_facebook = serializers.BooleanField(default=False)
    post_instagram = serializers.BooleanField(default=False)

    def validate_notes(self, value):
        return value.strip()

    def validate(self, attrs):
        if not (attrs["send_whatsapp"] or attrs["post_facebook"] or attrs["post_instagram"]):
            raise serializers.ValidationError({"send_whatsapp": "error.campaignChannelRequired"})
        if attrs["product_id"] is None and not attrs["notes"]:
            raise serializers.ValidationError({"notes": "error.aiDraftNeedsSubject"})
        return attrs


class AudienceMemberSerializer(serializers.Serializer):
    """One row of the preview sample - enough to recognise a real customer."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    phone = serializers.CharField()
    order_count = serializers.IntegerField()
    last_order = serializers.DateTimeField(allow_null=True)
    marketing_consent = serializers.CharField()


class CampaignRecipientSerializer(serializers.ModelSerializer):
    """
    A recipient, plus the link that sends their message.

    `whatsapp_url` is built per request rather than stored: it is derived
    from the snapshotted phone and message, and storing a 500-character URL
    per recipient would only duplicate them.
    """

    whatsapp_url = serializers.SerializerMethodField()

    class Meta:
        model = CampaignRecipient
        fields = [
            "id", "customer", "phone", "name", "message",
            "status", "sent_at", "error", "whatsapp_url",
        ]
        read_only_fields = fields

    def get_whatsapp_url(self, recipient):
        return whatsapp_link(recipient.phone, recipient.message)


class CampaignPostSerializer(serializers.ModelSerializer):
    """One channel's post: whether it is up, its link, and why not if it failed."""

    class Meta:
        model = CampaignPost
        fields = [
            "id", "channel", "status", "external_id", "permalink",
            "caption", "media_url", "error", "attempts", "posted_at", "updated_at",
        ]
        read_only_fields = fields


class SocialConnectionSerializer(serializers.ModelSerializer):
    """
    A connection as the admin sees it: which Page and Instagram account, and
    whether it works. There is no token field here on purpose - not masked,
    not "set: true" - so no future change to this class can leak one.
    """

    connected_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SocialConnection
        fields = [
            "id", "provider", "status",
            "page_id", "page_name", "ig_user_id", "ig_username",
            "last_error", "last_checked_at", "connected_at", "connected_by_name",
        ]
        read_only_fields = fields

    def get_connected_by_name(self, connection):
        return connection.connected_by.name if connection.connected_by_id else None


class MetaOAuthPagesSerializer(serializers.Serializer):
    """What Facebook's redirect handed the admin's callback page."""

    code = serializers.CharField(max_length=2000, error_messages={"blank": "error.metaOauthFailed", "required": "error.metaOauthFailed"})
    state = serializers.CharField(max_length=2000, error_messages={"blank": "error.metaOauthExpired", "required": "error.metaOauthExpired"})


class MetaOAuthConnectSerializer(serializers.Serializer):
    """The Page the owner picked, and the session oauth_pages gave them."""

    session = serializers.CharField(max_length=4000, error_messages={"blank": "error.metaOauthExpired", "required": "error.metaOauthExpired"})
    page_id = serializers.RegexField(
        r"^\d{5,32}$",
        error_messages={"invalid": "error.metaPageIdInvalid", "blank": "error.metaPageIdInvalid", "required": "error.metaPageIdInvalid"},
    )


class MetaConnectSerializer(serializers.Serializer):
    """What the owner pastes: the Page's numeric id and a token for it."""

    page_id = serializers.RegexField(
        r"^\d{5,32}$",
        error_messages={
            "invalid": "error.metaPageIdInvalid",
            "blank": "error.metaPageIdInvalid",
            "required": "error.metaPageIdInvalid",
        },
    )
    access_token = serializers.CharField(
        max_length=1000,
        trim_whitespace=True,
        # Never echoed back in an error, even a validation one.
        write_only=True,
        error_messages={
            "blank": "error.metaTokenRequired",
            "required": "error.metaTokenRequired",
            "max_length": "error.metaTokenInvalid",
        },
    )
