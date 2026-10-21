# Campaigns themselves were smoke-tested end to end against a running server
# (see docs/backend-plan.md's per-phase checklists). Social connections are
# tested here because they talk to Meta, which a smoke test can't stand in for.

from unittest.mock import patch

import requests
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch

from common import cloudinary
from imports.ai import AiDraftError

from . import publishing, social
from .models import CampaignAiDraft, ConnectionStatus, SocialConnection, SocialProvider

KEY = Fernet.generate_key().decode()
PAGE_ID = "1234567890"
USER_TOKEN = "EAAuser-token"
PAGE_TOKEN = "EAApage-token"
URL = "/api/marketing/connections/"


def make_staff(branch, name, role):
    staff = Staff(branch=branch, name=name, role=role, is_active=True)
    staff.set_pin("1234")
    staff.save()
    return staff


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._body


def graph(routes):
    """A stand-in for requests.get answering by (object id, access token)."""

    def fake_get(url, params=None, timeout=None):
        node = url.rsplit("/", 1)[-1]
        answer = routes.get((node, params["access_token"]))
        if answer is None:
            return FakeResponse({"error": {"code": 190, "message": "Invalid OAuth access token."}}, 400)
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)

    return patch("marketing.meta.requests.get", side_effect=fake_get)


# A Page with an Instagram account, reached with an admin's user token.
USER_TOKEN_ROUTES = {
    ("me", USER_TOKEN): {"id": "999"},
    (PAGE_ID, USER_TOKEN): {
        "id": PAGE_ID, "name": "Farzan Grill", "access_token": PAGE_TOKEN,
        "instagram_business_account": {"id": "17841400000000000"},
    },
    ("17841400000000000", PAGE_TOKEN): {"username": "farzangrill"},
}
# The same Page, checked again later with the stored Page token.
PAGE_TOKEN_ROUTES = {
    ("me", PAGE_TOKEN): {"id": PAGE_ID},
    (PAGE_ID, PAGE_TOKEN): {"id": PAGE_ID, "name": "Farzan Grill", "instagram_business_account": {"id": "17841400000000000"}},
    ("17841400000000000", PAGE_TOKEN): {"username": "farzangrill"},
}


@override_settings(SOCIAL_TOKEN_ENCRYPTION_KEY=KEY, META_APP_SECRET="")
class SocialConnectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.other_branch = Branch.objects.create(name_en="Other", name_ar="أخرى")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        cls.manager = make_staff(cls.branch, "Manager", StaffRole.MANAGER)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def connect(self, token=USER_TOKEN, page_id=PAGE_ID):
        return self.client.post(f"{URL}meta/", {"page_id": page_id, "access_token": token}, format="json")

    def test_user_token_is_swapped_for_the_page_token_and_stored_encrypted(self):
        with graph(USER_TOKEN_ROUTES):
            response = self.connect()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["page_name"], "Farzan Grill")
        self.assertEqual(response.data["ig_username"], "farzangrill")
        self.assertEqual(response.data["status"], ConnectionStatus.CONNECTED)
        self.assertEqual(response.data["connected_by_name"], "Owner")

        connection = SocialConnection.objects.get()
        self.assertNotIn(PAGE_TOKEN, connection.access_token_enc)
        self.assertEqual(social.page_token(connection), PAGE_TOKEN)

    def test_no_token_in_any_response(self):
        with graph(USER_TOKEN_ROUTES):
            created = self.connect()
        listed = self.client.get(URL)
        for body in (created.content, listed.content):
            self.assertNotIn(PAGE_TOKEN.encode(), body)
            self.assertNotIn(USER_TOKEN.encode(), body)
            self.assertNotIn(b"access_token", body)

    def test_page_token_is_used_as_is(self):
        with graph(PAGE_TOKEN_ROUTES):
            response = self.connect(token=PAGE_TOKEN)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(social.page_token(SocialConnection.objects.get()), PAGE_TOKEN)

    def test_page_without_instagram_still_connects(self):
        routes = {**PAGE_TOKEN_ROUTES, (PAGE_ID, PAGE_TOKEN): {"id": PAGE_ID, "name": "Farzan Grill"}}
        with graph(routes):
            response = self.connect(token=PAGE_TOKEN)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual((response.data["ig_user_id"], response.data["ig_username"]), ("", ""))

    def test_reconnecting_replaces_the_existing_connection(self):
        with graph(USER_TOKEN_ROUTES):
            self.connect()
        with graph(PAGE_TOKEN_ROUTES):
            self.connect(token=PAGE_TOKEN)
        self.assertEqual(SocialConnection.objects.filter(branch=self.branch).count(), 1)

    def test_rejected_token_is_not_saved(self):
        with graph({}):
            response = self.connect()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "error.metaTokenInvalid")
        self.assertFalse(SocialConnection.objects.exists())

    def test_user_who_is_not_a_page_admin_is_refused(self):
        routes = {**USER_TOKEN_ROUTES, (PAGE_ID, USER_TOKEN): {"id": PAGE_ID, "name": "Farzan Grill"}}
        with graph(routes):
            response = self.connect()
        self.assertEqual(response.data["error"], "error.metaNotPageAdmin")

    def test_meta_unreachable_is_a_502(self):
        with patch("marketing.meta.requests.get", side_effect=requests.ConnectionError):
            response = self.connect()
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["error"], "error.metaUnreachable")

    def test_invalid_page_id_and_missing_token_are_codes(self):
        response = self.client.post(f"{URL}meta/", {"page_id": "my-page", "access_token": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error.metaPageIdInvalid", str(response.data))
        self.assertIn("error.metaTokenRequired", str(response.data))

    @override_settings(SOCIAL_TOKEN_ENCRYPTION_KEY="")
    def test_no_key_refuses_before_calling_meta(self):
        with graph(USER_TOKEN_ROUTES) as fake:
            response = self.connect()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error"], "error.socialEncryptionNotConfigured")
        fake.assert_not_called()
        self.assertEqual(self.client.get(URL).data["encryption"], {"configured": False, "problem": "missing"})

    def test_verify_marks_a_revoked_token_expired(self):
        with graph(USER_TOKEN_ROUTES):
            connection_id = self.connect().data["id"]
        with graph({}):
            response = self.client.post(f"{URL}{connection_id}/verify/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ConnectionStatus.EXPIRED)
        self.assertTrue(response.data["last_error"])

    def test_verify_recovers_and_picks_up_a_newly_linked_instagram(self):
        routes = {**PAGE_TOKEN_ROUTES, (PAGE_ID, PAGE_TOKEN): {"id": PAGE_ID, "name": "Farzan Grill"}}
        with graph(routes):
            connection_id = self.connect(token=PAGE_TOKEN).data["id"]
        SocialConnection.objects.filter(pk=connection_id).update(status=ConnectionStatus.ERROR, last_error="x")
        with graph(PAGE_TOKEN_ROUTES):
            response = self.client.post(f"{URL}{connection_id}/verify/")
        self.assertEqual(response.data["status"], ConnectionStatus.CONNECTED)
        self.assertEqual(response.data["last_error"], "")
        self.assertEqual(response.data["ig_username"], "farzangrill")

    def test_disconnect_deletes_the_row(self):
        with graph(USER_TOKEN_ROUTES):
            connection_id = self.connect().data["id"]
        response = self.client.delete(f"{URL}{connection_id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SocialConnection.objects.exists())

    def test_manager_can_see_but_not_change(self):
        with graph(USER_TOKEN_ROUTES):
            connection_id = self.connect().data["id"]
        self.client.force_authenticate(user=self.manager)
        self.assertEqual(len(self.client.get(URL).data["connections"]), 1)
        with graph(USER_TOKEN_ROUTES):
            self.assertEqual(self.connect().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.post(f"{URL}{connection_id}/verify/").status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.delete(f"{URL}{connection_id}/").status_code, status.HTTP_403_FORBIDDEN)

    def test_other_restaurants_connection_is_invisible(self):
        other = SocialConnection.objects.create(branch=self.other_branch, provider=SocialProvider.META, page_id="555")
        self.assertEqual(self.client.get(URL).data["connections"], [])
        self.assertEqual(self.client.delete(f"{URL}{other.pk}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertIsNone(social.get_social_connection(self.branch))

    @override_settings(META_APP_SECRET="app-secret")
    def test_calls_are_signed_with_appsecret_proof(self):
        with graph(USER_TOKEN_ROUTES) as fake:
            self.connect()
        self.assertTrue(all("appsecret_proof" in call.kwargs["params"] for call in fake.call_args_list))


CLOUD = "demo-cloud"
IMAGE_URL = f"https://res.cloudinary.com/{CLOUD}/image/upload/v1/restaurant/campaigns/burger.jpg"
VIDEO_URL = f"https://res.cloudinary.com/{CLOUD}/video/upload/v1/restaurant/campaigns/reel.mp4"


@override_settings(CLOUDINARY_CLOUD_NAME=CLOUD, CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
class CampaignChannelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def create(self, **fields):
        return self.client.post("/api/campaigns/", {"name": "Weekend deal", **fields}, format="json")

    def test_old_style_payload_is_still_a_whatsapp_campaign(self):
        response = self.create(message_en="Hi {{name}}")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            (response.data["send_whatsapp"], response.data["post_facebook"], response.data["post_instagram"]),
            (True, False, False),
        )

    def test_at_least_one_channel(self):
        response = self.create(send_whatsapp=False, message_en="Hi")
        self.assertEqual(response.data["send_whatsapp"], ["error.campaignChannelRequired"])

    def test_post_only_campaign_needs_no_whatsapp_message(self):
        response = self.create(send_whatsapp=False, post_facebook=True, post_caption="  2 for 1 burgers tonight  ")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["post_caption"], "2 for 1 burgers tonight")

    def test_whatsapp_still_needs_its_message(self):
        response = self.create(post_facebook=True, post_caption="Tonight")
        self.assertEqual(response.data["message_en"], ["error.campaignMessageRequired"])

    def test_facebook_needs_a_caption_or_media(self):
        response = self.create(send_whatsapp=False, post_facebook=True)
        self.assertEqual(response.data["post_caption"], ["error.facebookPostEmpty"])
        response = self.create(send_whatsapp=False, post_facebook=True, media_type="image", media_url=IMAGE_URL)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_instagram_needs_media(self):
        response = self.create(send_whatsapp=False, post_instagram=True, post_caption="Tonight")
        self.assertEqual(response.data["media_url"], ["error.instagramNeedsMedia"])
        response = self.create(send_whatsapp=False, post_instagram=True, media_type="video", media_url=VIDEO_URL)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_instagram_hashtag_limit(self):
        caption = " ".join(f"#tag{i}" for i in range(31))
        response = self.create(send_whatsapp=False, post_instagram=True, media_type="image", media_url=IMAGE_URL, post_caption=caption)
        self.assertEqual(response.data["post_caption"], ["error.instagramTooManyHashtags"])
        # Facebook alone has no such limit.
        response = self.create(send_whatsapp=False, post_facebook=True, post_caption=caption)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_media_must_be_our_own_cloudinary_upload_of_the_stated_type(self):
        cases = [
            ("image", "https://evil.example.com/image/upload/x.jpg"),
            ("image", f"https://res.cloudinary.com/other-cloud/image/upload/v1/x.jpg"),
            ("video", IMAGE_URL),
            ("image", ""),
            ("", IMAGE_URL),
            ("gif", IMAGE_URL),
        ]
        for media_type, media_url in cases:
            with self.subTest(media_type=media_type, media_url=media_url):
                response = self.create(send_whatsapp=False, post_facebook=True, post_caption="x", media_type=media_type, media_url=media_url)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("error.campaignMediaInvalid", str(response.data))

    def test_patch_keeps_saved_channels_when_not_sent(self):
        campaign_id = self.create(send_whatsapp=False, post_instagram=True, media_type="image", media_url=IMAGE_URL).data["id"]
        response = self.client.patch(f"/api/campaigns/{campaign_id}/", {"post_caption": "New caption"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Clearing the media of an Instagram campaign is refused, not saved.
        response = self.client.patch(f"/api/campaigns/{campaign_id}/", {"media_type": "", "media_url": ""}, format="json")
        self.assertEqual(response.data["media_url"], ["error.instagramNeedsMedia"])

    def test_post_only_campaign_cannot_be_prepared(self):
        campaign_id = self.create(send_whatsapp=False, post_facebook=True, post_caption="x").data["id"]
        response = self.client.post(f"/api/campaigns/{campaign_id}/prepare/")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error"], "error.campaignNoWhatsapp")

    def test_config_reports_which_accounts_can_be_posted_to(self):
        def flags():
            body = self.client.get("/api/marketing/config/").data
            return body["facebook_connected"], body["instagram_connected"]

        self.assertEqual(flags(), (False, False))
        connection = SocialConnection.objects.create(branch=self.branch, provider=SocialProvider.META, page_id="1")
        self.assertEqual(flags(), (True, False))
        connection.ig_user_id = "178"
        connection.save()
        self.assertEqual(flags(), (True, True))
        connection.status = ConnectionStatus.EXPIRED
        connection.save()
        self.assertEqual(flags(), (False, False))

    def test_campaign_media_signatures_are_pinned_to_their_folder_and_type(self):
        video = self.client.post("/api/uploads/signature/", {"kind": "campaign_video"}, format="json").data
        self.assertTrue(video["upload_url"].endswith("/video/upload"))
        self.assertEqual(video["folder"], "restaurant/campaigns")
        self.assertEqual(video["max_bytes"], 100 * 1024 * 1024)
        self.assertEqual(video["allowed_types"], ["video/mp4", "video/quicktime"])

        image = self.client.post("/api/uploads/signature/", {"kind": "campaign_image"}, format="json").data
        self.assertTrue(image["upload_url"].endswith("/image/upload"))
        self.assertNotIn("image/svg+xml", image["allowed_types"])

        # Unknown kinds fall back to the ordinary product-image signature.
        plain = self.client.post("/api/uploads/signature/", {"kind": "anything"}, format="json").data
        self.assertEqual(plain["folder"], "restaurant")


def graph_posts(answers, permalink="https://www.facebook.com/farzan/posts/1"):
    """
    Stand-ins for requests.post and requests.get while publishing: `answers`
    maps an edge ("feed", "photos", "videos") to the response for it.
    """

    def fake_post(url, data=None, timeout=None):
        answer = answers.get(url.rsplit("/", 1)[-1])
        if answer is None:
            raise AssertionError(f"unexpected POST {url}")
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)

    def fake_get(url, params=None, timeout=None):
        return FakeResponse({"id": url.rsplit("/", 1)[-1], "permalink_url": permalink})

    return (
        patch("marketing.meta.requests.post", side_effect=fake_post),
        patch("marketing.meta.requests.get", side_effect=fake_get),
    )


TOKEN_REJECTED = FakeResponse({"error": {"code": 190, "message": "Session has expired."}}, 400)
TEMPORARY_FAILURE = FakeResponse({"error": {"code": 2, "message": "Temporary"}}, 500)


@override_settings(
    SOCIAL_TOKEN_ENCRYPTION_KEY=KEY, META_APP_SECRET="",
    CLOUDINARY_CLOUD_NAME=CLOUD, CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret",
)
class FacebookPublishingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.connection = SocialConnection.objects.create(
            branch=self.branch, provider=SocialProvider.META, page_id=PAGE_ID,
            access_token_enc=social._box.encrypt(PAGE_TOKEN),
        )

    def campaign(self, **fields):
        body = {"name": "Weekend deal", "send_whatsapp": False, "post_facebook": True, "post_caption": "2 for 1 tonight", **fields}
        response = self.client.post("/api/campaigns/", body, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data["id"]

    def publish(self, campaign_id, answers, **kwargs):
        post_patch, get_patch = graph_posts(answers, **kwargs)
        with post_patch as fake_post, get_patch:
            response = self.client.post(f"/api/campaigns/{campaign_id}/publish/")
        return response, fake_post

    def test_text_post_goes_to_the_feed_and_finishes_a_post_only_campaign(self):
        campaign_id = self.campaign()
        response, fake_post = self.publish(campaign_id, {"feed": {"id": f"{PAGE_ID}_1"}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        data = fake_post.call_args.kwargs["data"]
        self.assertEqual(data["message"], "2 for 1 tonight")
        self.assertEqual(data["access_token"], PAGE_TOKEN)
        [post] = response.data["posts"]
        self.assertEqual(
            (post["channel"], post["status"], post["external_id"], post["permalink"], post["attempts"]),
            ("facebook", "posted", f"{PAGE_ID}_1", "https://www.facebook.com/farzan/posts/1", 1),
        )
        self.assertEqual(post["caption"], "2 for 1 tonight")
        self.assertEqual(response.data["campaign"]["status"], "sent")
        self.assertIsNotNone(response.data["campaign"]["sent_at"])
        self.assertFalse(response.data["campaign"]["is_editable"])

    def test_image_goes_to_photos_and_links_the_page_post(self):
        campaign_id = self.campaign(media_type="image", media_url=IMAGE_URL)
        response, fake_post = self.publish(campaign_id, {"photos": {"id": "photo1", "post_id": f"{PAGE_ID}_2"}})
        data = fake_post.call_args.kwargs["data"]
        self.assertEqual((data["url"], data["caption"]), (IMAGE_URL, "2 for 1 tonight"))
        self.assertEqual(response.data["posts"][0]["external_id"], f"{PAGE_ID}_2")
        self.assertEqual(response.data["posts"][0]["media_url"], IMAGE_URL)

    def test_video_goes_to_videos_and_a_relative_permalink_is_made_absolute(self):
        campaign_id = self.campaign(media_type="video", media_url=VIDEO_URL)
        response, fake_post = self.publish(
            campaign_id, {"videos": {"id": "video1"}}, permalink="/farzan/videos/video1/"
        )
        data = fake_post.call_args.kwargs["data"]
        self.assertEqual((data["file_url"], data["description"]), (VIDEO_URL, "2 for 1 tonight"))
        self.assertEqual(response.data["posts"][0]["permalink"], "https://www.facebook.com/farzan/videos/video1/")

    def test_publishing_twice_posts_once(self):
        campaign_id = self.campaign()
        self.publish(campaign_id, {"feed": {"id": f"{PAGE_ID}_1"}})
        response, fake_post = self.publish(campaign_id, {"feed": {"id": f"{PAGE_ID}_9"}})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error"], "error.campaignAlreadyPublished")
        fake_post.assert_not_called()

    def test_rejected_token_fails_the_post_and_expires_the_connection(self):
        campaign_id = self.campaign()
        response, _ = self.publish(campaign_id, {"feed": TOKEN_REJECTED})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [post] = response.data["posts"]
        self.assertEqual((post["status"], post["error"]), ("failed", "error.metaTokenInvalid"))
        self.assertEqual(response.data["campaign"]["status"], "draft")
        self.assertTrue(response.data["campaign"]["is_editable"])
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, ConnectionStatus.EXPIRED)

        # Now nothing can post until the owner reconnects.
        response, fake_post = self.publish(campaign_id, {"feed": {"id": "x"}})
        self.assertEqual(response.data["error"], "error.metaNotConnected")
        fake_post.assert_not_called()

    def test_meta_refusal_keeps_metas_own_message(self):
        campaign_id = self.campaign()
        refusal = FakeResponse({"error": {"code": 200, "message": "Requires pages_manage_posts permission"}}, 403)
        response, _ = self.publish(campaign_id, {"feed": refusal})
        self.assertEqual(response.data["posts"][0]["error"], "Requires pages_manage_posts permission")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, ConnectionStatus.CONNECTED)

    def test_retry_only_failed_posts(self):
        campaign_id = self.campaign()
        retry_url = f"/api/campaigns/{campaign_id}/posts/facebook/retry/"
        self.assertEqual(self.client.post(retry_url).data["error"], "error.postNotFailed")

        self.publish(campaign_id, {"feed": TEMPORARY_FAILURE})
        post_patch, get_patch = graph_posts({"feed": {"id": f"{PAGE_ID}_3"}})
        with post_patch, get_patch:
            response = self.client.post(retry_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [post] = response.data["posts"]
        self.assertEqual((post["status"], post["attempts"], post["error"]), ("posted", 2, ""))
        self.assertEqual(response.data["campaign"]["status"], "sent")
        self.assertEqual(self.client.post(retry_url).data["error"], "error.postNotFailed")

    def test_a_live_post_locks_editing_and_deleting(self):
        # A WhatsApp campaign too: the post goes up while recipients are
        # still a draft rule, and the campaign keeps its own status.
        campaign_id = self.campaign(send_whatsapp=True, message_en="Hi {{name}}")
        response, _ = self.publish(campaign_id, {"feed": {"id": f"{PAGE_ID}_1"}})
        self.assertEqual(response.data["campaign"]["status"], "draft")
        self.assertFalse(response.data["campaign"]["is_editable"])

        response = self.client.patch(f"/api/campaigns/{campaign_id}/", {"post_caption": "Changed"}, format="json")
        self.assertEqual(response.data["error"], "error.campaignNotEditable")
        response = self.client.delete(f"/api/campaigns/{campaign_id}/")
        self.assertEqual(response.data["error"], "error.campaignNotDeletable")

    def test_refusals_before_anything_is_posted(self):
        whatsapp_only = self.campaign(post_facebook=False, send_whatsapp=True, message_en="Hi")
        response, fake_post = self.publish(whatsapp_only, {})
        self.assertEqual(response.data["error"], "error.campaignNoPosts")

        cancelled = self.campaign()
        self.client.post(f"/api/campaigns/{cancelled}/cancel/")
        response, _ = self.publish(cancelled, {})
        self.assertEqual(response.data["error"], "error.campaignCancelled")

        self.connection.delete()
        response, _ = self.publish(self.campaign(), {})
        self.assertEqual(response.data["error"], "error.metaNotConnected")
        fake_post.assert_not_called()

    def test_posts_lists_only_ticked_channels(self):
        campaign_id = self.campaign()
        posts_url = f"/api/campaigns/{campaign_id}/posts/"
        self.assertEqual(self.client.get(posts_url).data["posts"], [])
        self.publish(campaign_id, {"feed": TEMPORARY_FAILURE})
        self.assertEqual(len(self.client.get(posts_url).data["posts"]), 1)
        # Still editable after a failure; unticking Facebook drops its row.
        response = self.client.patch(
            f"/api/campaigns/{campaign_id}/",
            {"post_facebook": False, "send_whatsapp": True, "message_en": "Hi"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.client.get(posts_url).data["posts"], [])


IG_USER_ID = "17841400000000000"


def meta_routes(routes):
    """
    Stand-ins for requests.get and requests.post answering by (method, path
    after the API version). `routes` is read on every call, so a test can
    change an answer (a container finishing) between requests. Unknown
    routes fail the test loudly rather than answering something plausible.
    """

    def answer(method, url):
        path = url.split(".com/", 1)[1].split("/", 1)[1]
        result = routes.get((method, path))
        if result is None:
            raise AssertionError(f"unexpected {method} {path}")
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, FakeResponse) else FakeResponse(result)

    return (
        patch("marketing.meta.requests.get", side_effect=lambda url, params=None, timeout=None: answer("GET", url)),
        patch("marketing.meta.requests.post", side_effect=lambda url, data=None, timeout=None: answer("POST", url)),
    )


def instagram_routes(container_status="FINISHED"):
    return {
        ("GET", f"{IG_USER_ID}/content_publishing_limit"): {"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]},
        ("POST", f"{IG_USER_ID}/media"): {"id": "container1"},
        ("GET", "container1"): {"status_code": container_status},
        ("POST", f"{IG_USER_ID}/media_publish"): {"id": "media1"},
        ("GET", "media1"): {"permalink": "https://www.instagram.com/p/abc/"},
    }


@override_settings(
    SOCIAL_TOKEN_ENCRYPTION_KEY=KEY, META_APP_SECRET="",
    CLOUDINARY_CLOUD_NAME=CLOUD, CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret",
)
class InstagramPublishingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.connection = SocialConnection.objects.create(
            branch=self.branch, provider=SocialProvider.META, page_id=PAGE_ID, ig_user_id=IG_USER_ID,
            access_token_enc=social._box.encrypt(PAGE_TOKEN),
        )

    def campaign(self, **fields):
        body = {
            "name": "Weekend deal", "send_whatsapp": False, "post_instagram": True,
            "post_caption": "2 for 1 tonight #burgers", "media_type": "image", "media_url": IMAGE_URL, **fields,
        }
        response = self.client.post("/api/campaigns/", body, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data["id"]

    def call(self, routes, method, url):
        get_patch, post_patch = meta_routes(routes)
        with get_patch as fake_get, post_patch as fake_post:
            response = getattr(self.client, method)(url)
        return response, fake_get, fake_post

    def publish(self, campaign_id, routes):
        return self.call(routes, "post", f"/api/campaigns/{campaign_id}/publish/")

    def poll(self, campaign_id, routes):
        return self.call(routes, "get", f"/api/campaigns/{campaign_id}/posts/")[0]

    def test_image_is_converted_padded_and_published_at_once(self):
        campaign_id = self.campaign()
        response, _, fake_post = self.publish(campaign_id, instagram_routes())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        container_data = fake_post.call_args_list[0].kwargs["data"]
        self.assertEqual(container_data["caption"], "2 for 1 tonight #burgers")
        self.assertEqual(
            container_data["image_url"],
            f"https://res.cloudinary.com/{CLOUD}/image/upload/"
            f"{publishing.INSTAGRAM_IMAGE_TRANSFORMATION}/v1/restaurant/campaigns/burger.jpg",
        )
        self.assertEqual(fake_post.call_args_list[1].kwargs["data"]["creation_id"], "container1")

        [post] = response.data["posts"]
        self.assertEqual(
            (post["channel"], post["status"], post["external_id"], post["permalink"]),
            ("instagram", "posted", "media1", "https://www.instagram.com/p/abc/"),
        )
        self.assertEqual(response.data["campaign"]["status"], "sent")

    def test_png_and_webp_are_delivered_as_jpeg(self):
        url = f"https://res.cloudinary.com/{CLOUD}/image/upload/v1/restaurant/campaigns/menu.webp"
        self.assertTrue(cloudinary.transformed_url(url, "q_auto", extension="jpg").endswith("/upload/q_auto/v1/restaurant/campaigns/menu.jpg"))

    def test_video_processes_until_meta_is_done(self):
        campaign_id = self.campaign(media_type="video", media_url=VIDEO_URL)
        routes = instagram_routes(container_status="IN_PROGRESS")
        response, _, fake_post = self.publish(campaign_id, routes)
        data = fake_post.call_args.kwargs["data"]
        self.assertEqual((data["media_type"], data["video_url"]), ("REELS", VIDEO_URL))
        self.assertEqual(response.data["posts"][0]["status"], "processing")
        # Processing already counts as live: no editing, no second publish.
        self.assertFalse(response.data["campaign"]["is_editable"])
        self.assertEqual(self.publish(campaign_id, routes)[0].data["error"], "error.campaignAlreadyPublished")

        self.assertEqual(self.poll(campaign_id, routes).data["posts"][0]["status"], "processing")
        # Looking failing changes nothing.
        routes[("GET", "container1")] = requests.ConnectionError()
        self.assertEqual(self.poll(campaign_id, routes).data["posts"][0]["status"], "processing")

        routes[("GET", "container1")] = {"status_code": "FINISHED"}
        response = self.poll(campaign_id, routes)
        self.assertEqual(response.data["posts"][0]["status"], "posted")
        self.assertEqual(response.data["campaign"]["status"], "sent")

    def test_rejected_video_fails_with_a_code(self):
        campaign_id = self.campaign(media_type="video", media_url=VIDEO_URL)
        routes = instagram_routes(container_status="IN_PROGRESS")
        self.publish(campaign_id, routes)
        routes[("GET", "container1")] = {"status_code": "ERROR", "status": "Video too short"}
        [post] = self.poll(campaign_id, routes).data["posts"]
        self.assertEqual((post["status"], post["error"]), ("failed", "error.instagramMediaRejected"))

        # Retry starts over with a fresh container.
        routes[("GET", "container1")] = {"status_code": "FINISHED"}
        response, _, _ = self.call(routes, "post", f"/api/campaigns/{campaign_id}/posts/instagram/retry/")
        self.assertEqual(response.data["posts"][0]["status"], "posted")
        self.assertEqual(response.data["posts"][0]["attempts"], 2)

    def test_page_without_instagram_fails_only_that_channel(self):
        self.connection.ig_user_id = ""
        self.connection.save()
        campaign_id = self.campaign(post_facebook=True)
        routes = {
            ("POST", f"{PAGE_ID}/photos"): {"id": "photo1", "post_id": f"{PAGE_ID}_2"},
            ("GET", f"{PAGE_ID}_2"): {"permalink_url": "https://www.facebook.com/p/2"},
        }
        response, _, _ = self.publish(campaign_id, routes)
        posts = {post["channel"]: post for post in response.data["posts"]}
        self.assertEqual(posts["facebook"]["status"], "posted")
        self.assertEqual((posts["instagram"]["status"], posts["instagram"]["error"]), ("failed", "error.instagramNotConnected"))
        # Not every ticked channel is up, so the campaign isn't sent yet.
        self.assertEqual(response.data["campaign"]["status"], "draft")
        # Facebook got the original image; only Instagram's copy is transformed.
        self.assertEqual(response.data["posts"][0]["media_url"], IMAGE_URL)

    def test_publishing_limit_is_checked_first(self):
        campaign_id = self.campaign()
        routes = instagram_routes()
        routes[("GET", f"{IG_USER_ID}/content_publishing_limit")] = {
            "data": [{"quota_usage": 100, "config": {"quota_total": 100}}]
        }
        response, _, fake_post = self.publish(campaign_id, routes)
        self.assertEqual(response.data["posts"][0]["error"], "error.instagramLimitReached")
        fake_post.assert_not_called()


@override_settings(CLOUDINARY_CLOUD_NAME=CLOUD, CLOUDINARY_API_KEY="key", CLOUDINARY_API_SECRET="secret")
class WhatsappMediaLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from customers.models import Customer

        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        Customer.objects.create(branch=cls.branch, phone="0501234567", name="Sara")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_preview_and_recipients_end_with_the_media_link(self):
        rule = {"min_orders": 0, "message_en": "Hi {{name}}, 2 for 1 tonight", "media_url": IMAGE_URL}
        preview = self.client.post("/api/campaigns/preview/", rule, format="json").data
        self.assertEqual(preview["message_preview"], f"Hi Sara, 2 for 1 tonight\n\n{IMAGE_URL}")

        response = self.client.post(
            "/api/campaigns/", {"name": "Deal", "media_type": "image", **rule}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        campaign_id = response.data["id"]
        self.assertEqual(self.client.post(f"/api/campaigns/{campaign_id}/prepare/").status_code, status.HTTP_200_OK)
        [recipient] = self.client.get(f"/api/campaigns/{campaign_id}/recipients/").data["results"]
        self.assertEqual(recipient["message"], f"Hi Sara, 2 for 1 tonight\n\n{IMAGE_URL}")
        self.assertIn("%0A%0Ahttps%3A//res.cloudinary.com/", recipient["whatsapp_url"])

    def test_no_media_no_link(self):
        preview = self.client.post("/api/campaigns/preview/", {"min_orders": 0, "message_en": "Hi"}, format="json").data
        self.assertEqual(preview["message_preview"], "Hi")

    def test_message_and_link_must_fit_the_cap(self):
        room = 1000 - 2 - len(IMAGE_URL)
        body = {"name": "Deal", "media_type": "image", "media_url": IMAGE_URL}
        response = self.client.post("/api/campaigns/", {**body, "message_ar": "ا" * (room + 1)}, format="json")
        self.assertEqual(response.data["message_ar"], ["error.messageTooLongForMedia"])
        response = self.client.post("/api/campaigns/", {**body, "message_ar": "ا" * room}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        # A post-only campaign never builds a WhatsApp message, so no limit.
        response = self.client.post(
            "/api/campaigns/",
            {**body, "send_whatsapp": False, "post_facebook": True, "message_ar": "ا" * 999},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


AI_ANSWER = {
    "post_caption": "Zinger Tuesdays: 25 instead of 32, 12:00-16:00. Save 50%! #zinger #farzan",
    "message_en": "Hi {{name}}, our Zinger is 25 every Tuesday from 12:00.",
    "message_ar": "مرحباً {{name}}، الزنجر بـ ٢٥ كل ثلاثاء من 12:00.",
}


@override_settings(AI_API_KEY="test-key", AI_CAMPAIGN_DRAFTS_PER_DAY=2)
class CampaignAiDraftTests(TestCase):
    URL = "/api/campaigns/ai-draft/"

    @classmethod
    def setUpTestData(cls):
        from datetime import time
        from decimal import Decimal

        from catalog.models import Category, Deal, DealWindow, Product

        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        category = Category.objects.create(branch=cls.branch, name_en="Burgers", name_ar="برجر")
        cls.product = Product.objects.create(
            branch=cls.branch, category=category, name_en="Zinger", name_ar="زنجر",
            price=Decimal("32.00"), cost_price=Decimal("12.00"),
        )
        deal = Deal.objects.create(product=cls.product, price=Decimal("25.00"))
        DealWindow.objects.create(deal=deal, day="tue", start_time=time(12), end_time=time(16))

        other = Branch.objects.create(name_en="Other", name_ar="أخرى")
        cls.other_product = Product.objects.create(
            branch=other, category=Category.objects.create(branch=other, name_en="X", name_ar="X"),
            name_en="Other", name_ar="Other", price=1, cost_price=1,
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def ask(self, answer=None, error=None):
        def fake_ask(chat, name, schema, messages, extra_body=None, temperature=0.2):
            if error:
                raise AiDraftError(error)
            return dict(answer or AI_ANSWER), (100, 50)

        return patch("imports.ai.ask", side_effect=fake_ask)

    def test_draft_from_a_deal_flags_numbers_it_made_up(self):
        with self.ask() as fake:
            response = self.client.post(
                self.URL, {"product_id": self.product.pk, "post_facebook": True}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["post_caption"], AI_ANSWER["post_caption"])
        self.assertEqual(response.data["message_ar"], AI_ANSWER["message_ar"])
        # 25, 32, 12:00 and 16:00 are the product's own; the Arabic ٢٥ is
        # read as 25. Only the invented 50% is pointed out.
        self.assertEqual(response.data["unverified_numbers"], ["50"])
        self.assertEqual(response.data["left_today"], 1)

        prompt = fake.call_args.args[3][1]["content"]
        self.assertIn('"deal_price": "25"', prompt)
        self.assertIn('"day": "Tuesday"', prompt)
        self.assertNotIn("cost", prompt)  # cost price stays private

        log = CampaignAiDraft.objects.get()
        self.assertEqual((log.succeeded, log.prompt_tokens, log.completion_tokens), (True, 100, 50))

    def test_only_the_ticked_texts_come_back(self):
        with self.ask():
            post_only = self.client.post(
                self.URL, {"notes": "New zinger", "send_whatsapp": False, "post_instagram": True}, format="json"
            ).data
        self.assertEqual((post_only["message_en"], post_only["message_ar"]), ("", ""))
        self.assertTrue(post_only["post_caption"])

        with self.ask():
            whatsapp_only = self.client.post(self.URL, {"notes": "New zinger"}, format="json").data
        self.assertEqual(whatsapp_only["post_caption"], "")

    def test_numbers_in_the_notes_are_not_flagged(self):
        with self.ask({"post_caption": "Family box for 99, feeds 4", "message_en": "", "message_ar": ""}):
            response = self.client.post(
                self.URL, {"notes": "family box 99 riyals for 4 people", "send_whatsapp": False, "post_facebook": True}, format="json"
            )
        self.assertEqual(response.data["unverified_numbers"], [])

    def test_refusals(self):
        self.assertEqual(
            self.client.post(self.URL, {}, format="json").data["notes"], ["error.aiDraftNeedsSubject"]
        )
        response = self.client.post(self.URL, {"product_id": self.other_product.pk}, format="json")
        self.assertEqual(response.data["product_id"], ["error.aiDraftProductInvalid"])
        response = self.client.post(self.URL, {"notes": "x", "send_whatsapp": False}, format="json")
        self.assertEqual(response.data["send_whatsapp"], ["error.campaignChannelRequired"])
        with override_settings(AI_API_KEY=""):
            response = self.client.post(self.URL, {"notes": "x"}, format="json")
        self.assertEqual((response.status_code, response.data["error"]), (503, "imports.aiNotConfigured"))
        self.assertFalse(CampaignAiDraft.objects.exists())

    def test_failures_count_toward_the_daily_allowance(self):
        with self.ask(error="imports.aiTimeout"):
            response = self.client.post(self.URL, {"notes": "x"}, format="json")
        self.assertEqual((response.status_code, response.data["error"]), (502, "imports.aiTimeout"))
        with self.ask():
            self.assertEqual(self.client.post(self.URL, {"notes": "x"}, format="json").status_code, 200)
        with self.ask() as fake:
            response = self.client.post(self.URL, {"notes": "x"}, format="json")
        self.assertEqual((response.status_code, response.data["error"]), (429, "imports.aiDailyLimit"))
        fake.assert_not_called()

        config = self.client.get("/api/marketing/config/").data
        self.assertEqual((config["ai_enabled"], config["ai_drafts_left_today"]), (True, 0))


OAUTH_REDIRECT = "https://admin.example.com/admin/marketing/accounts/facebook-callback"
LONG_TOKEN = "EAAlong-lived-user-token"


def oauth_graph(exchange_error=None):
    """requests.get for the whole login: code -> short token -> long token -> Pages."""
    routes = {
        ("me", LONG_TOKEN): {"id": "999"},
        ("me/accounts", LONG_TOKEN): {"data": [
            {"id": PAGE_ID, "name": "Farzan Grill", "tasks": ["ADVERTISE", "CREATE_CONTENT", "MODERATE"],
             "instagram_business_account": {"id": "17841400000000000", "username": "farzangrill"}},
            {"id": "5550000000", "name": "Fan page", "tasks": ["MODERATE"]},
        ]},
        (PAGE_ID, LONG_TOKEN): {
            "id": PAGE_ID, "name": "Farzan Grill", "access_token": PAGE_TOKEN,
            "instagram_business_account": {"id": "17841400000000000"},
        },
        ("17841400000000000", PAGE_TOKEN): {"username": "farzangrill"},
    }

    def fake_get(url, params=None, timeout=None):
        path = url.split(".com/", 1)[1].split("/", 1)[1]
        if path == "oauth/access_token":
            if params["client_secret"] != "app-secret":
                return FakeResponse({"error": {"code": 1, "message": "bad secret"}}, 400)
            if exchange_error:
                return FakeResponse({"error": {"code": 100, "message": exchange_error}}, 400)
            if params.get("code") == "the-code" and params.get("redirect_uri") == OAUTH_REDIRECT:
                return FakeResponse({"access_token": "EAAshort", "expires_in": 3600})
            if params.get("grant_type") == "fb_exchange_token" and params.get("fb_exchange_token") == "EAAshort":
                return FakeResponse({"access_token": LONG_TOKEN, "expires_in": 5184000})
            return FakeResponse({"error": {"code": 100, "message": "Invalid verification code format."}}, 400)
        answer = routes.get((path, params.get("access_token")))
        if answer is None:
            return FakeResponse({"error": {"code": 190, "message": "Invalid OAuth access token."}}, 400)
        return FakeResponse(answer)

    return patch("marketing.meta.requests.get", side_effect=fake_get)


@override_settings(
    SOCIAL_TOKEN_ENCRYPTION_KEY=KEY, META_APP_ID="1234", META_APP_SECRET="app-secret",
    META_OAUTH_REDIRECT_URI=OAUTH_REDIRECT,
)
class MetaOAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Farzan", name_ar="فرزان")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        cls.manager = make_staff(cls.branch, "Manager", StaffRole.MANAGER)
        other_branch = Branch.objects.create(name_en="Other", name_ar="أخرى")
        cls.other_owner = make_staff(other_branch, "Other owner", StaffRole.OWNER)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def start(self):
        from urllib.parse import parse_qs, urlparse

        response = self.client.post(f"{URL}meta/oauth/start/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        url = urlparse(response.data["url"])
        return url, {key: values[0] for key, values in parse_qs(url.query).items()}

    def pages(self, state, code="the-code", **graph_kwargs):
        with oauth_graph(**graph_kwargs):
            return self.client.post(f"{URL}meta/oauth/pages/", {"code": code, "state": state}, format="json")

    def test_dialog_url(self):
        url, query = self.start()
        self.assertEqual(url.netloc, "www.facebook.com")
        self.assertTrue(url.path.endswith("/dialog/oauth"))
        self.assertEqual((query["client_id"], query["redirect_uri"], query["response_type"]), ("1234", OAUTH_REDIRECT, "code"))
        self.assertIn("pages_manage_posts", query["scope"].split(","))
        self.assertIn("instagram_content_publish", query["scope"].split(","))
        self.assertTrue(self.client.get(URL).data["oauth"]["configured"])

    def test_login_lists_pages_then_connects_the_picked_one(self):
        _, query = self.start()
        response = self.pages(query["state"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [(page["id"], page["can_post"], page["ig_username"]) for page in response.data["pages"]],
            [(PAGE_ID, True, "farzangrill"), ("5550000000", False, "")],
        )
        # The user token travels only encrypted.
        session = response.data["session"]
        self.assertNotIn(LONG_TOKEN, str(response.data))

        with oauth_graph():
            response = self.client.post(
                f"{URL}meta/oauth/connect/", {"session": session, "page_id": PAGE_ID}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        connection = SocialConnection.objects.get()
        self.assertEqual((connection.page_name, connection.ig_username, connection.connected_by), ("Farzan Grill", "farzangrill", self.owner))
        # The Page token read with the long-lived user token is what's kept.
        self.assertEqual(social.page_token(connection), PAGE_TOKEN)

    def test_state_and_session_belong_to_the_owner_who_started(self):
        _, query = self.start()
        intruder = APIClient()
        intruder.force_authenticate(user=self.other_owner)
        with oauth_graph():
            response = intruder.post(f"{URL}meta/oauth/pages/", {"code": "the-code", "state": query["state"]}, format="json")
        self.assertEqual(response.data["error"], "error.metaOauthExpired")

        session = self.pages(query["state"]).data["session"]
        with oauth_graph():
            response = intruder.post(f"{URL}meta/oauth/connect/", {"session": session, "page_id": PAGE_ID}, format="json")
        self.assertEqual(response.data["error"], "error.metaOauthExpired")
        self.assertFalse(SocialConnection.objects.exists())

    def test_tampered_or_old_state_is_refused(self):
        _, query = self.start()
        self.assertEqual(self.pages(query["state"] + "x").data["error"], "error.metaOauthExpired")
        with patch("marketing.social.STATE_MAX_AGE", -1):
            self.assertEqual(self.pages(query["state"]).data["error"], "error.metaOauthExpired")

    def test_a_used_or_bad_code_says_log_in_again(self):
        _, query = self.start()
        response = self.pages(query["state"], exchange_error="This authorization code has been used.")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            (response.data["error"], response.data["detail"]),
            ("error.metaOauthFailed", "This authorization code has been used."),
        )

    def test_owner_only_and_needs_configuration(self):
        manager = APIClient()
        manager.force_authenticate(user=self.manager)
        self.assertEqual(manager.post(f"{URL}meta/oauth/start/").status_code, status.HTTP_403_FORBIDDEN)

        with override_settings(META_APP_ID=""):
            self.assertFalse(self.client.get(URL).data["oauth"]["configured"])
            response = self.client.post(f"{URL}meta/oauth/start/")
        self.assertEqual(response.data["error"], "error.metaOauthNotConfigured")

        with override_settings(SOCIAL_TOKEN_ENCRYPTION_KEY=""):
            response = self.client.post(f"{URL}meta/oauth/start/")
        self.assertEqual(response.data["error"], "error.socialEncryptionNotConfigured")
