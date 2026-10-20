from rest_framework import serializers

from .models import AudienceConsent, Campaign, CampaignRecipient
from .services import whatsapp_link

# Longest sensible recency window. Two years of "hasn't ordered since" is
# already a dead customer; the cap exists so a typo'd 9999 can't quietly
# produce an empty audience that looks like a bug.
MAX_RECENCY_DAYS = 730


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
            "id", "name", "channel", "status",
            "message_en", "message_ar", "fallback_name",
            "audience_consent", "min_orders", "ordered_within_days", "not_ordered_for_days",
            "scheduled_for", "prepared_at", "sent_at",
            "recipient_counts", "is_editable", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "status", "prepared_at", "sent_at",
            "recipient_counts", "is_editable", "created_by_name", "created_at", "updated_at",
        ]

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

    def validate(self, attrs):
        # Fall back to the saved value on a PATCH that only touches one field.
        def current(field):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, None)

        if not (current("message_en") or current("message_ar")):
            raise serializers.ValidationError({"message_en": "error.campaignMessageRequired"})

        within, since = current("ordered_within_days"), current("not_ordered_for_days")
        if within and since and since >= within:
            raise serializers.ValidationError({"not_ordered_for_days": "error.impossibleAudienceWindow"})
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
