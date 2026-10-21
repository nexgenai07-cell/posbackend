"""
Posting a campaign to the restaurant's own Facebook Page and Instagram account.

Runs synchronously inside the request, like WhatsApp's /send/ — there is no
task queue in this project. Each channel's outcome is recorded on its
CampaignPost row rather than raised, so one channel failing never hides what
happened on another.

Instagram is two steps: create a media container, then publish it once Meta
has finished processing it. An image is normally ready at once; a video
isn't, so its post sits in `processing` and advance_processing() moves it on
each time the admin polls GET .../posts/.

The view holds the campaign row lock while this runs; that is what stops two
clicks on "Publish" (or two polls) from posting twice.
"""

import logging

from django.utils import timezone

from common import cloudinary

from . import meta
from .models import (
    LIVE_POST_STATUSES,
    CampaignPost,
    CampaignStatus,
    ConnectionStatus,
    MediaType,
    PostChannel,
    PostStatus,
)

logger = logging.getLogger(__name__)

# Meta downloads the video from its URL before answering, which for a large
# file takes far longer than an ordinary Graph call.
VIDEO_TIMEOUT_SECONDS = 120

# Instagram takes JPEG only, 4:5 (portrait) to 1.91:1 (landscape), at most
# 1440 wide, and refuses anything outside that ratio. Rather than turning a
# square-ish phone photo away, Cloudinary pads an image that is too tall or
# too wide out to the nearest allowed ratio, filling with a colour taken from
# the image's edges, and the ".jpg" extension converts PNG/WebP to JPEG.
INSTAGRAM_IMAGE_TRANSFORMATION = (
    "if_ar_lt_0.8/c_pad,ar_4:5,b_auto/if_end/"
    "if_ar_gt_1.91/c_pad,ar_1.91,b_auto/if_end/"
    "c_limit,w_1440/q_auto"
)

# Container states (GET /{container_id}?fields=status_code).
CONTAINER_READY = "FINISHED"
CONTAINER_PUBLISHED = "PUBLISHED"
CONTAINER_FAILED = ("ERROR", "EXPIRED")


def ticked_channels(campaign):
    """The public channels this campaign is meant to go to."""
    channels = []
    if campaign.post_facebook:
        channels.append(PostChannel.FACEBOOK)
    if campaign.post_instagram:
        channels.append(PostChannel.INSTAGRAM)
    return channels


def publishable_channels(campaign):
    """Ticked channels that can be posted to by this code today."""
    return [channel for channel in ticked_channels(campaign) if channel in PUBLISHERS]


# -- Facebook ------------------------------------------------------------------


def publish_facebook(post, connection, token):
    """
    Put the campaign on the Page. Three Graph endpoints for three kinds of
    post; each makes a normal Page post that shows on the timeline. The
    permalink is looked up afterwards because none of them return it.
    """
    page_id = connection.page_id
    if post.media_url and post.campaign.media_type == MediaType.VIDEO:
        result = meta.graph_post(
            f"{page_id}/videos",
            token,
            {"file_url": post.media_url, "description": post.caption},
            timeout=VIDEO_TIMEOUT_SECONDS,
        )
    elif post.media_url:
        result = meta.graph_post(f"{page_id}/photos", token, {"url": post.media_url, "caption": post.caption})
    else:
        result = meta.graph_post(f"{page_id}/feed", token, {"message": post.caption})

    # /photos answers with the photo's id plus the Page post's id; the post
    # is what has a timeline link. /feed and /videos answer with one id.
    post.external_id = str(result.get("post_id") or result.get("id") or "")
    post.permalink = _permalink(post.external_id, token, "permalink_url")
    post.status = PostStatus.POSTED


# -- Instagram -----------------------------------------------------------------


def publish_instagram(post, connection, token):
    """
    Create the media container, then publish it straight away if Meta is
    already done with it (usual for an image) or leave the post processing
    (usual for a video).
    """
    ig_user_id = connection.ig_user_id
    if not ig_user_id:
        # The Page has no Instagram professional account linked (or it was
        # linked after connecting and "Check again" hasn't been pressed).
        raise meta.MetaError("error.instagramNotConnected")
    _check_publishing_limit(ig_user_id, token)

    campaign = post.campaign
    if campaign.media_type == MediaType.VIDEO:
        data = {"media_type": "REELS", "video_url": post.media_url, "caption": post.caption}
        timeout = VIDEO_TIMEOUT_SECONDS
    else:
        data = {"image_url": post.media_url, "caption": post.caption}
        timeout = meta.REQUEST_TIMEOUT_SECONDS
    result = meta.graph_post(f"{ig_user_id}/media", token, data, timeout=timeout)

    post.container_id = str(result.get("id") or "")
    if not post.container_id:
        raise meta.MetaError("error.metaRequestFailed", "Instagram did not return a media container.")
    post.status = PostStatus.PROCESSING
    _advance_instagram(post, connection, token)


def _check_publishing_limit(ig_user_id, token):
    """
    Instagram allows a limited number of API posts per account per 24 hours.
    Asking first gives a clear reason instead of a container that can never
    be published. If the question itself fails, publishing goes ahead and
    Meta's own answer decides.
    """
    try:
        usage = meta.graph_get(f"{ig_user_id}/content_publishing_limit", token, {"fields": "quota_usage,config"})
    except meta.MetaError as error:
        if error.token_invalid:
            raise
        logger.warning("Could not read Instagram publishing limit for %s: %s", ig_user_id, error.detail or error.code)
        return
    rows = usage.get("data") or [{}]
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    total = (row.get("config") or {}).get("quota_total")
    if isinstance(total, int) and isinstance(row.get("quota_usage"), int) and row["quota_usage"] >= total:
        raise meta.MetaError("error.instagramLimitReached")


def _advance_instagram(post, connection, token):
    """
    Look at the container once and act on what it says. Raises MetaError for
    a container Meta gave up on; leaves the post processing while it works.
    """
    try:
        container = meta.graph_get(post.container_id, token, {"fields": "status_code,status"})
    except meta.MetaError as error:
        if error.code == "error.metaUnreachable":
            # Only *looking* failed; the container is fine. The next poll
            # asks again.
            return
        raise
    state = container.get("status_code") or ""
    if state in CONTAINER_FAILED:
        # The post keeps the translatable code; Meta's sentence for why
        # (unsupported codec, too short, ...) goes to the log.
        logger.warning("Instagram container %s %s: %s", post.container_id, state, container.get("status"))
        raise meta.MetaError("error.instagramMediaRejected", str(container.get("status") or state))
    if state == CONTAINER_PUBLISHED:
        # Already published (a request that timed out after Meta acted).
        # The media id isn't recoverable from the container; the post is up.
        post.status = PostStatus.POSTED
        return
    if state != CONTAINER_READY:
        return

    result = meta.graph_post(f"{connection.ig_user_id}/media_publish", token, {"creation_id": post.container_id})
    post.external_id = str(result.get("id") or "")
    post.permalink = _permalink(post.external_id, token, "permalink")
    post.status = PostStatus.POSTED


def _permalink(external_id, token, field):
    """The public link to a live post, or "" — never fails a post that worked."""
    if not external_id:
        return ""
    try:
        link = meta.graph_get(external_id, token, {"fields": field}).get(field) or ""
    except meta.MetaError as error:
        logger.warning("Could not read permalink for %s: %s", external_id, error.detail or error.code)
        return ""
    # Facebook videos answer with a path on facebook.com rather than a URL.
    if link.startswith("/"):
        link = f"https://www.facebook.com{link}"
    return link[:500]


PUBLISHERS = {
    PostChannel.FACEBOOK: publish_facebook,
    PostChannel.INSTAGRAM: publish_instagram,
}


# -- recording outcomes ----------------------------------------------------------


def media_url_for(campaign, channel):
    """The media URL as this channel should fetch it."""
    if channel == PostChannel.INSTAGRAM and campaign.media_type == MediaType.IMAGE and campaign.media_url:
        return cloudinary.transformed_url(campaign.media_url, INSTAGRAM_IMAGE_TRANSFORMATION, extension="jpg")
    return campaign.media_url


def publish_post(post, connection, token):
    """Attempt one channel's post from scratch and record the outcome on `post`."""
    campaign = post.campaign
    post.caption = campaign.post_caption
    post.media_url = media_url_for(campaign, post.channel)
    post.container_id = ""
    post.external_id = ""
    post.permalink = ""
    post.attempts += 1
    _record(post, connection, lambda: PUBLISHERS[post.channel](post, connection, token))
    return post


def advance_processing(post, connection, token):
    """One check on an Instagram post Meta is still processing."""
    _record(post, connection, lambda: _advance_instagram(post, connection, token))
    return post


def _record(post, connection, attempt):
    """
    Run `attempt` and save what happened. A token Meta rejects marks the
    connection expired as well, so the Social accounts page and the config
    flags stop offering it until the owner reconnects.
    """
    try:
        attempt()
    except meta.MetaError as error:
        post.status = PostStatus.FAILED
        # Meta's own sentence says more than "Meta refused the request";
        # every other code is something the admin can translate.
        post.error = (error.detail if error.code == "error.metaRequestFailed" and error.detail else error.code)[:255]
        if error.token_invalid:
            connection.status = ConnectionStatus.EXPIRED
            connection.last_error = error.detail or error.code
            connection.last_checked_at = timezone.now()
            connection.save(update_fields=["status", "last_error", "last_checked_at", "updated_at"])
    else:
        post.error = ""
        if post.status == PostStatus.POSTED:
            post.posted_at = timezone.now()
    post.save()


def pending_posts(campaign):
    """
    The rows publishing should attempt now: one per publishable channel,
    created on first use, skipping anything already up or processing.
    """
    posts = []
    for channel in publishable_channels(campaign):
        post, _ = CampaignPost.objects.get_or_create(campaign=campaign, channel=channel)
        if post.status not in LIVE_POST_STATUSES:
            posts.append(post)
    return posts


def refresh_campaign_status(campaign):
    """
    A post-only campaign is sent once every ticked channel is posted. A
    campaign that also goes to WhatsApp keeps the status its recipients
    give it (views._refresh_status); the posts are shown beside it.
    """
    if campaign.send_whatsapp or campaign.status in (CampaignStatus.SENT, CampaignStatus.CANCELLED):
        return
    channels = ticked_channels(campaign)
    posted = set(
        campaign.posts.filter(status=PostStatus.POSTED).values_list("channel", flat=True)
    )
    if channels and all(channel in posted for channel in channels):
        campaign.status = CampaignStatus.SENT
        campaign.sent_at = campaign.sent_at or timezone.now()
        campaign.save(update_fields=["status", "sent_at", "updated_at"])
