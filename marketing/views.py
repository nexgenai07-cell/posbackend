from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwner, IsOwnerOrManager
from common.crypto import EncryptionKeyMismatch, EncryptionNotConfigured
from common.filters import choice_filter, ordering_filter, text_filter
from common.mixins import BranchScopedQuerysetMixin

from catalog.models import Product
from imports.ai import AiDraftError

from . import ai, publishing, social
from .meta import MetaError
from .models import (
    LIVE_POST_STATUSES,
    Campaign,
    CampaignAiDraft,
    CampaignStatus,
    ConnectionStatus,
    PostStatus,
    RecipientStatus,
    SocialConnection,
)
from .senders import SenderError, cloud_api_sender, whatsapp_cloud_configured
from .serializers import (
    AudienceMemberSerializer,
    CampaignAiDraftSerializer,
    CampaignPostSerializer,
    CampaignPreviewSerializer,
    CampaignRecipientSerializer,
    CampaignSerializer,
    MetaConnectSerializer,
    MetaOAuthConnectSerializer,
    MetaOAuthPagesSerializer,
    SocialConnectionSerializer,
)
from .services import (
    AUDIENCE_SAMPLE_SIZE,
    MAX_RECIPIENTS,
    audience_queryset,
    build_recipients,
    sendable_customers,
    preview_message,
    recipient_counts,
)

# How many messages one /send/ call will push. The Cloud API is called
# synchronously - there is no task queue in this project, deliberately - so a
# campaign of 800 is several calls rather than one request that outlives its
# own timeout. The admin loops until nothing is pending.
SEND_BATCH_LIMIT = 100


class CampaignViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    WhatsApp marketing campaigns.

    Lifecycle: draft (editable, audience is a live rule) -> prepare() freezes
    the audience into recipients -> ready -> either staff tap each wa.me link
    and mark them sent, or send() pushes them through the Cloud API once that
    is configured -> sent.

    A campaign can also (or only) post to Facebook and Instagram; those
    channels have no audience, so prepare() is WhatsApp's step alone, and
    publish() is theirs. A post-only campaign goes draft -> sent once every
    ticked channel is posted.

    Filters: ?status=, ?search= (name/message/caption), ?ordering=.
    """

    queryset = Campaign.objects.select_related("branch", "created_by")
    serializer_class = CampaignSerializer
    permission_classes = [IsOwnerOrManager]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        queryset = choice_filter(queryset, params, "status", allowed=CampaignStatus.values)
        queryset = text_filter(queryset, params, "search", ["name", "message_en", "message_ar", "post_caption"])
        return ordering_filter(
            queryset, params,
            allowed={"name": "name", "status": "status", "created_at": "created_at", "sent_at": "sent_at"},
            default=["-created_at"],
        )

    def perform_create(self, serializer):
        serializer.save(branch=self.request.user.branch, created_by=self.request.user)

    def update(self, request, *args, **kwargs):
        if not self.get_object().is_editable:
            # Past draft the recipient rows hold a copy of the message, and a
            # live post holds a copy of the caption. Editing either here would
            # leave the record describing words nobody got.
            return Response({"error": "error.campaignNotEditable"}, status=status.HTTP_409_CONFLICT)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        campaign = self.get_object()
        if campaign.status in (CampaignStatus.SENDING, CampaignStatus.SENT) or campaign.has_live_posts:
            # A campaign that reached real phones, or a public Page, is a
            # record of what the restaurant said, and PDPL questions get
            # asked about exactly that. Cancel it instead.
            return Response({"error": "error.campaignNotDeletable"}, status=status.HTTP_409_CONFLICT)
        return super().destroy(request, *args, **kwargs)

    # -- audience ------------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="preview")
    def preview(self, request):
        """
        POST /api/campaigns/preview/

        How many customers an unsaved rule matches, a sample of who they are,
        and the message as the first of them would read it. Takes the form's
        current values so the count answers the question while the rule is
        still being edited, not after it has been saved.
        """
        serializer = CampaignPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = serializer.validated_data

        audience = audience_queryset(
            request.user.branch,
            consent=rule["audience_consent"],
            min_orders=rule["min_orders"],
            ordered_within_days=rule["ordered_within_days"],
            not_ordered_for_days=rule["not_ordered_for_days"],
        )
        # The same filtering prepare() will apply, so the number shown here is
        # the number that will actually be built - not a COUNT that silently
        # includes customers whose phone cannot be messaged.
        members = sendable_customers(audience)
        sample = members[:AUDIENCE_SAMPLE_SIZE]

        # An unsaved stand-in so the preview runs through exactly the same
        # rendering the real send will use, rather than a second copy of it.
        draft = Campaign(
            branch=request.user.branch,
            message_en=rule["message_en"],
            message_ar=rule["message_ar"],
            fallback_name=rule["fallback_name"],
            media_url=rule["media_url"],
        )
        return Response({
            "count": len(members),
            "max_recipients": MAX_RECIPIENTS,
            "sample": AudienceMemberSerializer(sample, many=True).data,
            "message_preview": preview_message(draft, sample[0] if sample else None),
        })

    # -- AI drafting ---------------------------------------------------------

    @action(detail=False, methods=["post"], url_path="ai-draft")
    def ai_draft(self, request):
        """
        POST /api/campaigns/ai-draft/
        {product_id?, notes, send_whatsapp, post_facebook, post_instagram}

        A first draft of the caption and WhatsApp messages for the form to
        fill in. Nothing is saved except the usage log row. Numbers the AI
        wrote that aren't in the product data or the notes come back in
        `unverified_numbers` for the form to point out.
        """
        branch = request.user.branch
        if not ai.enabled():
            return Response({"error": "imports.aiNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        serializer = CampaignAiDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        product = None
        if params["product_id"] is not None:
            product = (
                Product.objects.filter(branch=branch, pk=params["product_id"])
                .select_related("category", "deal")
                .first()
            )
            if product is None:
                return Response({"product_id": ["error.aiDraftProductInvalid"]}, status=status.HTTP_400_BAD_REQUEST)
        if _ai_drafts_left_today(branch) == 0:
            return Response({"error": "imports.aiDailyLimit"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # Logged before the call: a request that fails still costs money and
        # still counts toward the day's allowance.
        log = CampaignAiDraft.objects.create(branch=branch, requested_by=request.user)
        try:
            result, (log.prompt_tokens, log.completion_tokens) = ai.draft(
                branch,
                product=product,
                notes=params["notes"],
                whatsapp=params["send_whatsapp"],
                post=params["post_facebook"] or params["post_instagram"],
            )
        except AiDraftError as error:
            log.error = error.code
            log.save(update_fields=["error"])
            return Response({"error": error.code}, status=status.HTTP_502_BAD_GATEWAY)
        log.succeeded = True
        log.save(update_fields=["succeeded", "prompt_tokens", "completion_tokens"])
        return Response({**result, "left_today": _ai_drafts_left_today(branch)})

    # -- lifecycle -----------------------------------------------------------

    @action(detail=True, methods=["post"])
    def prepare(self, request, pk=None):
        """Freeze the audience into recipients and make the campaign sendable."""
        campaign = self.get_object()
        if campaign.status != CampaignStatus.DRAFT:
            return Response({"error": "error.campaignNotDraft"}, status=status.HTTP_409_CONFLICT)
        if not campaign.send_whatsapp:
            # Freezing an audience is for messaging customers; a post-only
            # campaign has none to freeze.
            return Response({"error": "error.campaignNoWhatsapp"}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            created = build_recipients(campaign)
            if not created:
                # Nothing to freeze. Left as a draft so the rule can be
                # widened, rather than becoming a "ready" campaign with an
                # empty list that looks sendable.
                return Response({"error": "error.emptyAudience"}, status=status.HTTP_409_CONFLICT)
            campaign.status = CampaignStatus.READY
            campaign.prepared_at = timezone.now()
            campaign.save(update_fields=["status", "prepared_at", "updated_at"])

        return Response(self.get_serializer(campaign).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        """
        Back to draft, discarding the frozen recipient list.

        Only while nothing has been sent. The rows are hard-deleted, not
        soft-deleted: they are a snapshot being thrown away rather than
        history, and a soft-deleted row would still hold the
        (campaign, customer) unique constraint and block re-preparing.
        """
        campaign = self.get_object()
        if campaign.status != CampaignStatus.READY:
            return Response({"error": "error.campaignNotReady"}, status=status.HTTP_409_CONFLICT)
        if campaign.recipients.exclude(status=RecipientStatus.PENDING).exists():
            return Response({"error": "error.campaignAlreadyStarted"}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            campaign.recipients.all().hard_delete()
            campaign.status = CampaignStatus.DRAFT
            campaign.prepared_at = None
            campaign.save(update_fields=["status", "prepared_at", "updated_at"])
        return Response(self.get_serializer(campaign).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        campaign = self.get_object()
        if campaign.status == CampaignStatus.SENT:
            return Response({"error": "error.campaignAlreadySent"}, status=status.HTTP_409_CONFLICT)
        campaign.status = CampaignStatus.CANCELLED
        campaign.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(campaign).data)

    # -- recipients ----------------------------------------------------------

    @action(detail=True, methods=["get"])
    def recipients(self, request, pk=None):
        """
        GET /api/campaigns/{id}/recipients/?status=

        Paginated, each row carrying the wa.me link that sends it. This is
        the manual send screen's whole data source.
        """
        campaign = self.get_object()
        queryset = campaign.recipients.all()
        queryset = choice_filter(queryset, request.query_params, "status", allowed=RecipientStatus.values)
        page = self.paginate_queryset(queryset)
        serializer = CampaignRecipientSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="mark-sent")
    def mark_sent(self, request, pk=None):
        """
        POST /api/campaigns/{id}/mark-sent/  {"recipient_ids": [...]} | {"all": true}

        The manual path's "I tapped the link and sent it" step. `all` has to
        be asked for explicitly - defaulting to it would turn a mis-click
        into a campaign that claims it reached everybody.
        """
        campaign = self.get_object()
        if campaign.status not in (CampaignStatus.READY, CampaignStatus.SENDING):
            return Response({"error": "error.campaignNotSendable"}, status=status.HTTP_409_CONFLICT)

        pending = campaign.recipients.filter(status=RecipientStatus.PENDING)
        ids = request.data.get("recipient_ids")
        if request.data.get("all") is not True:
            if not isinstance(ids, list) or not ids:
                return Response({"error": "error.recipientsRequired"}, status=status.HTTP_400_BAD_REQUEST)
            pending = pending.filter(pk__in=[i for i in ids if isinstance(i, int)])

        marked = pending.update(status=RecipientStatus.SENT, sent_at=timezone.now(), updated_at=timezone.now())
        _refresh_status(campaign)
        return Response({"marked": marked, "campaign": self.get_serializer(campaign).data})

    # -- automated sending ---------------------------------------------------

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """
        POST /api/campaigns/{id}/send/

        Pushes up to SEND_BATCH_LIMIT pending recipients through the WhatsApp
        Cloud API. Returns what it did and what is still pending, so the
        caller knows whether to call again.

        Unavailable until the Cloud API is configured - the manual wa.me path
        is the one that works out of the box, and answering with a clear code
        is better than a half-built automated send.
        """
        campaign = self.get_object()
        sender = cloud_api_sender()
        if sender is None:
            return Response({"error": "error.whatsappNotConfigured"}, status=status.HTTP_409_CONFLICT)
        if campaign.status not in (CampaignStatus.READY, CampaignStatus.SENDING):
            return Response({"error": "error.campaignNotSendable"}, status=status.HTTP_409_CONFLICT)

        try:
            sender.check_audience(campaign)
        except SenderError as error:
            return Response({"error": error.code}, status=status.HTTP_409_CONFLICT)

        batch = list(campaign.recipients.filter(status=RecipientStatus.PENDING)[:SEND_BATCH_LIMIT])
        sent = failed = 0
        for recipient in batch:
            result = sender.send(recipient)
            recipient.status = RecipientStatus.SENT if result.ok else RecipientStatus.FAILED
            recipient.sent_at = timezone.now() if result.ok else None
            recipient.external_id = result.external_id
            recipient.error = "" if result.ok else result.error
            recipient.save(update_fields=["status", "sent_at", "external_id", "error", "updated_at"])
            sent, failed = (sent + 1, failed) if result.ok else (sent, failed + 1)

        _refresh_status(campaign)
        return Response({
            "sent": sent,
            "failed": failed,
            "pending": campaign.recipients.filter(status=RecipientStatus.PENDING).count(),
            "campaign": self.get_serializer(campaign).data,
        })

    @action(detail=True, methods=["post"], url_path="retry-failed")
    def retry_failed(self, request, pk=None):
        """Put failed recipients back in the queue so /send/ picks them up."""
        campaign = self.get_object()
        reset = campaign.recipients.filter(status=RecipientStatus.FAILED).update(
            status=RecipientStatus.PENDING, error="", updated_at=timezone.now()
        )
        _refresh_status(campaign)
        return Response({"reset": reset, "campaign": self.get_serializer(campaign).data})

    # -- public posts --------------------------------------------------------

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """
        POST /api/campaigns/{id}/publish/

        Posts to every ticked channel that isn't already up, right now and
        publicly. Channels already posted (or processing) are left alone, so
        calling it again only retries what failed. Answers with every post
        and the campaign, whose status may have moved to sent.
        """
        campaign = self.get_object()
        with transaction.atomic():
            # The lock is what makes a double click post once: the second
            # request waits here, then finds the first one's posts live.
            campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
            if campaign.status == CampaignStatus.CANCELLED:
                return Response({"error": "error.campaignCancelled"}, status=status.HTTP_409_CONFLICT)
            channels = publishing.publishable_channels(campaign)
            if not channels:
                return Response({"error": "error.campaignNoPosts"}, status=status.HTTP_409_CONFLICT)
            live = set(campaign.posts.filter(status__in=LIVE_POST_STATUSES).values_list("channel", flat=True))
            if all(channel in live for channel in channels):
                return Response({"error": "error.campaignAlreadyPublished"}, status=status.HTTP_409_CONFLICT)

            connection, token, refusal = _publishing_token(campaign.branch)
            if refusal:
                return refusal
            for post in publishing.pending_posts(campaign):
                publishing.publish_post(post, connection, token)
            publishing.refresh_campaign_status(campaign)
        return self._posts_response(campaign)

    @action(detail=True, methods=["get"])
    def posts(self, request, pk=None):
        """
        GET /api/campaigns/{id}/posts/ - one row per ticked channel attempted so far.

        Also the clock for Instagram videos: each call checks any post Meta
        is still processing once and publishes it when it is ready. The
        admin polls this every few seconds while something is processing.
        """
        campaign = self.get_object()
        if not campaign.posts.filter(status=PostStatus.PROCESSING).exists():
            return self._posts_response(campaign)

        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
            processing = list(campaign.posts.filter(status=PostStatus.PROCESSING))
            connection, token, refusal = _publishing_token(campaign.branch)
            # No working connection: show the posts as they stand rather than
            # failing a read; publishing will say what is wrong.
            if not refusal:
                for post in processing:
                    publishing.advance_processing(post, connection, token)
                publishing.refresh_campaign_status(campaign)
        return self._posts_response(campaign)

    @action(detail=True, methods=["post"], url_path=r"posts/(?P<channel>facebook|instagram)/retry")
    def retry_post(self, request, pk=None, channel=None):
        """POST /api/campaigns/{id}/posts/{channel}/retry/ - one failed post, again."""
        campaign = self.get_object()
        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
            if campaign.status == CampaignStatus.CANCELLED:
                return Response({"error": "error.campaignCancelled"}, status=status.HTTP_409_CONFLICT)
            post = campaign.posts.filter(channel=channel).first()
            if (
                post is None
                or post.status != PostStatus.FAILED
                or channel not in publishing.publishable_channels(campaign)
            ):
                return Response({"error": "error.postNotFailed"}, status=status.HTTP_409_CONFLICT)

            connection, token, refusal = _publishing_token(campaign.branch)
            if refusal:
                return refusal
            publishing.publish_post(post, connection, token)
            publishing.refresh_campaign_status(campaign)
        return self._posts_response(campaign)

    def _posts_response(self, campaign):
        # Only ticked channels: a failed attempt on a channel that was later
        # unticked is no longer part of this campaign.
        posts = campaign.posts.filter(channel__in=publishing.ticked_channels(campaign))
        return Response({
            "posts": CampaignPostSerializer(posts, many=True).data,
            "campaign": self.get_serializer(campaign).data,
        })


def _ai_drafts_left_today(branch):
    used = CampaignAiDraft.objects.filter(branch=branch, created_at__date=timezone.localdate()).count()
    return max(settings.AI_CAMPAIGN_DRAFTS_PER_DAY - used, 0)


def _publishing_token(branch):
    """
    (connection, page token, None), or (None, None, a 409 to answer with)
    when the restaurant has nothing that can post right now.
    """
    connection = social.get_social_connection(branch)
    if connection is None or connection.status != ConnectionStatus.CONNECTED:
        return None, None, Response({"error": "error.metaNotConnected"}, status=status.HTTP_409_CONFLICT)
    try:
        token = social.page_token(connection)
    except (EncryptionNotConfigured, EncryptionKeyMismatch):
        return None, None, Response(
            {"error": "error.socialEncryptionNotConfigured"}, status=status.HTTP_409_CONFLICT
        )
    return connection, token, None


def _refresh_status(campaign):
    """
    Move the campaign along to match its recipients.

    Derived rather than set by each caller, so the manual path and the API
    path can never disagree about when a campaign counts as finished.
    """
    counts = recipient_counts(campaign)
    if not counts["total"]:
        # No recipients at all means the campaign was never prepared (or was
        # reopened). "Everything has been sent" would be the wrong reading.
        return
    if counts[RecipientStatus.PENDING]:
        new_status = CampaignStatus.SENDING if counts[RecipientStatus.SENT] else CampaignStatus.READY
    else:
        new_status = CampaignStatus.SENT

    fields = ["status", "updated_at"]
    campaign.status = new_status
    if new_status == CampaignStatus.SENT and campaign.sent_at is None:
        campaign.sent_at = timezone.now()
        fields.append("sent_at")
    campaign.save(update_fields=fields)


class MarketingConfigView(APIView):
    """
    GET /api/marketing/config/

    What the admin needs to know before it renders a campaign screen: whether
    automated sending exists yet, which country code a bare local number will
    be given in a wa.me link, and which social accounts can be posted to.
    """

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        connection = social.get_social_connection(request.user.branch)
        facebook = bool(connection and connection.status == ConnectionStatus.CONNECTED)
        return Response({
            "whatsapp_cloud_configured": whatsapp_cloud_configured(),
            "facebook_connected": facebook,
            # Instagram rides on the Page connection, so it needs both.
            "instagram_connected": facebook and bool(connection.ig_user_id),
            "ai_enabled": ai.enabled(),
            "ai_drafts_left_today": _ai_drafts_left_today(request.user.branch),
            "default_country_code": settings.MARKETING_DEFAULT_COUNTRY_CODE,
            "max_recipients": MAX_RECIPIENTS,
            "send_batch_limit": SEND_BATCH_LIMIT,
        })


class SocialConnectionViewSet(BranchScopedQuerysetMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """
    The restaurant's own Facebook / Instagram connection.

    GET    /api/marketing/connections/             status, no secrets
    POST   /api/marketing/connections/meta/        {page_id, access_token}
    POST   /api/marketing/connections/meta/oauth/start/    -> {url} (Facebook Login)
    POST   /api/marketing/connections/meta/oauth/pages/    {code, state} -> {pages, session}
    POST   /api/marketing/connections/meta/oauth/connect/  {session, page_id}
    POST   /api/marketing/connections/{id}/verify/ ask Meta again
    DELETE /api/marketing/connections/{id}/        disconnect

    Managers can see whether an account is connected - they write the
    campaigns that will post to it - but only the owner can connect or
    disconnect one, the same line ZATCA credentials draw.
    """

    queryset = SocialConnection.objects.select_related("connected_by")
    serializer_class = SocialConnectionSerializer

    def get_permissions(self):
        if self.action == "list":
            return [IsOwnerOrManager()]
        return [IsOwner()]

    def list(self, request):
        configured, problem = social.encryption_status()
        return Response({
            "encryption": {"configured": configured, "problem": problem},
            "oauth": {"configured": social.oauth_configured()},
            "connections": self.get_serializer(self.get_queryset(), many=True).data,
        })

    @action(detail=False, methods=["post"], url_path="meta")
    def connect_meta(self, request):
        serializer = MetaConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            connection = social.connect_meta(
                request.user.branch,
                request.user,
                serializer.validated_data["page_id"],
                serializer.validated_data["access_token"],
            )
        except EncryptionNotConfigured:
            return Response({"error": "error.socialEncryptionNotConfigured"}, status=status.HTTP_409_CONFLICT)
        except MetaError as error:
            return _meta_error_response(error)
        return Response(self.get_serializer(connection).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="meta/oauth/start")
    def oauth_start(self, request):
        try:
            url = social.oauth_start(request.user)
        except social.OAuthError as error:
            return Response({"error": error.code}, status=status.HTTP_409_CONFLICT)
        except EncryptionNotConfigured:
            return Response({"error": "error.socialEncryptionNotConfigured"}, status=status.HTTP_409_CONFLICT)
        return Response({"url": url})

    @action(detail=False, methods=["post"], url_path="meta/oauth/pages")
    def oauth_pages(self, request):
        serializer = MetaOAuthPagesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            pages, session = social.oauth_pages(
                request.user, serializer.validated_data["code"], serializer.validated_data["state"]
            )
        except social.OAuthError as error:
            return Response({"error": error.code}, status=status.HTTP_409_CONFLICT)
        except (EncryptionNotConfigured, EncryptionKeyMismatch):
            return Response({"error": "error.socialEncryptionNotConfigured"}, status=status.HTTP_409_CONFLICT)
        except MetaError as error:
            return _meta_error_response(error)
        return Response({"pages": pages, "session": session})

    @action(detail=False, methods=["post"], url_path="meta/oauth/connect")
    def oauth_connect(self, request):
        serializer = MetaOAuthConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            connection = social.oauth_connect(
                request.user, serializer.validated_data["session"], serializer.validated_data["page_id"]
            )
        except social.OAuthError as error:
            return Response({"error": error.code}, status=status.HTTP_409_CONFLICT)
        except (EncryptionNotConfigured, EncryptionKeyMismatch):
            return Response({"error": "error.socialEncryptionNotConfigured"}, status=status.HTTP_409_CONFLICT)
        except MetaError as error:
            return _meta_error_response(error)
        return Response(self.get_serializer(connection).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        connection = self.get_object()
        try:
            connection = social.check_connection(connection)
        except (EncryptionNotConfigured, EncryptionKeyMismatch):
            # The key changed or went missing since the token was saved. The
            # token is unrecoverable either way; reconnecting is the fix.
            return Response({"error": "error.socialEncryptionNotConfigured"}, status=status.HTTP_409_CONFLICT)
        except MetaError as error:
            return _meta_error_response(error)
        return Response(self.get_serializer(connection).data)

    def perform_destroy(self, instance):
        # Hard delete: see SocialConnection's docstring.
        instance.delete()


def _meta_error_response(error):
    # 502 for "Meta didn't answer", 400 for "Meta said no": the first is
    # worth retrying as-is, the second needs a different token or Page.
    code = status.HTTP_502_BAD_GATEWAY if error.code == "error.metaUnreachable" else status.HTTP_400_BAD_REQUEST
    return Response({"error": error.code, "detail": error.detail}, status=code)
