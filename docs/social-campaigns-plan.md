# Social Campaigns — Build Plan

Reference doc for extending `marketing/` so one campaign (text + optional image/video) can go to WhatsApp, Facebook and Instagram from tick boxes. Spans `restaurant-backend` and `restaurant-admin`. Check items off here as they're built — this file is the source of truth for scope, not the chat it came from. **A new session should be able to pick up the next unchecked milestone from this file alone.**

Status: **M1–M7 built** (2026-10-21), uncommitted in both repos. Not yet migrated on the shared (Neon) database (0002–0005; the user runs all migrations once at the end — don't stop for them), not yet tried against a real Facebook Page or Instagram account, and the AI draft not yet tried with the real NVIDIA key (Arabic quality unchecked). Next: the **Later** items; first, the user's real-account checks (bottom of "Pending on the user's side").

## Goal

Staff create a campaign, attach an image or video if they want, and tick where it goes: ☐ WhatsApp ☐ Facebook ☐ Instagram. Each restaurant connects its own accounts. Later, AI drafts the caption (e.g. when a new deal is added).

## Decisions

- **Per-restaurant credentials live in the database, encrypted.** Only platform-wide values stay in env: Meta App ID/Secret, Graph API version and the token encryption key.
- **`Branch` is the restaurant (Option A).** There is no `Restaurant` model — branding already lives on `Branch`, `Staff` belongs to one branch, and `ZatcaSettings` is OneToOne with `Branch`. `SocialConnection` follows the same pattern. All code reads it through `marketing.social.get_social_connection(branch)`, so introducing a `Restaurant` model later only changes that function and one migration.
- **WhatsApp stays manual (`wa.me`) for now.** A `wa.me` link cannot carry media, so the media URL goes in the message text (M5). Real media sending waits for the Cloud API + an approved media template.
- **Facebook and Instagram are public posts, not per-customer messages** — no audience, no consent rules. They get their own per-channel record (`CampaignPost`, M3) next to the per-recipient one (`CampaignRecipient`).
- **Connect / verify / disconnect is owner-only** (like ZATCA credentials); managers can see the status and write campaigns.
- **No task queue.** Everything Meta-facing runs synchronously in the request, like the existing WhatsApp `/send/`; slow work (Instagram video processing) is advanced by the admin polling an endpoint.
- **AI reuses the existing NVIDIA setup** (`imports/ai.py`, OpenAI-compatible, `AI_*` settings). AI only drafts; nothing is posted without staff approval.

## Where things are (built so far)

Backend (`restaurant-backend`):

| File | What |
|---|---|
| `common/crypto.py` | `SecretBox(setting, label)` — Fernet/MultiFernet encryption, shared by ZATCA and social tokens |
| `zatca/crypto.py` | Now a thin wrapper over `SecretBox("ZATCA_ENCRYPTION_KEY")`, same public functions as before |
| `common/cloudinary.py` | Upload `KINDS` (`image`, `document`, `campaign_image`, `campaign_video`), `is_own_upload(url, resource_type)`, `transformed_url(url, transformation, extension)` |
| `common/views.py` | `POST /api/uploads/signature/` takes `{"kind": ...}` |
| `marketing/models.py` | `SocialProvider`, `ConnectionStatus`, `SocialConnection`, `MediaType`, new `Campaign` fields |
| `marketing/meta.py` | Graph API client: `graph_get`, `graph_post(path, token, data, timeout)`, OAuth (`OAUTH_SCOPES`, `oauth_dialog_url`, `exchange_code`, `list_pages`), `MetaError` (codes below), `resolve_page(page_id, token)`, `appsecret_proof` |
| `marketing/publishing.py` | `PUBLISHERS` (Facebook + Instagram), `publish_facebook`, `publish_instagram` (limit check → container → publish if `FINISHED`, else `processing`), `advance_processing`, `INSTAGRAM_IMAGE_TRANSFORMATION`, `media_url_for`, `publish_post` (records outcome on the row, expires the connection on a token error), `pending_posts`, `ticked_channels` / `publishable_channels`, `refresh_campaign_status` (post-only → `sent`) |
| `marketing/social.py` | `encryption_status`, `get_social_connection`, `page_token`, `connect_meta`, `check_connection`; OAuth: `oauth_configured`, `oauth_start`, `oauth_pages`, `oauth_connect`, `OAuthError` |
| `marketing/serializers.py` | `SocialConnectionSerializer` (no token field), `MetaConnectSerializer`, `CampaignPostSerializer`, channel/media rules in `CampaignSerializer.validate` |
| `marketing/views.py` | `SocialConnectionViewSet`, `prepare()` refuses post-only campaigns, config flags, `publish` / `posts` / `retry_post` actions, `_publishing_token` |
| `marketing/ai.py` | "Write with AI": `draft(branch, product=, notes=, whatsapp=, post=)`, `product_context`, unverified-number check |
| `imports/ai.py` | Now exposes `client(timeout)` and `ask(chat, name, schema, messages, extra_body, temperature)` (was `_ask`) for `marketing/ai.py` |
| `marketing/migrations/0002_social_connection.py`, `0003_campaign_channels_and_media.py`, `0004_campaign_post.py`, `0005_campaign_ai_draft.py` | M1, M2, M3, M6 |
| `marketing/tests.py` | `SocialConnectionTests`, `CampaignChannelTests`, `FacebookPublishingTests`, `InstagramPublishingTests` (Graph calls mocked with `patch("marketing.meta.requests.get"/".post")`; helpers `graph_posts`, and `meta_routes` keyed by (method, path) for multi-step flows) |

Admin (`restaurant-admin`):

| File | What |
|---|---|
| `src/pages/admin/marketing/SocialAccountsPage.tsx` | `/admin/marketing/accounts` — "Connect with Facebook" (when configured; paste form collapses behind a link), connect form, status, Check again / Replace token / Disconnect, WhatsApp "Manual" card |
| `src/pages/admin/marketing/FacebookCallbackPage.tsx` | `/admin/marketing/accounts/facebook-callback` — exchanges code once, Page picker, connect |
| `src/pages/admin/marketing/CampaignFormPage.tsx` | "Where to send" tick boxes (`ChannelOption`), post caption + media upload, WhatsApp sections only when WhatsApp is ticked |
| `src/pages/admin/marketing/CampaignDetailPage.tsx` | Channel pills, post preview; Prepare/recipients only for WhatsApp; Edit follows `isEditable`; renders `CampaignPostsCard` |
| `src/components/marketing/CampaignPostsCard.tsx` | Per-channel post rows (pill, "View post", error + Retry, processing / no-Instagram hints), Publish with confirm, polls `getCampaignPosts` every 5 s while a post is `processing` |
| `src/pages/admin/marketing/CampaignsPage.tsx` | "Social accounts" header button |
| `src/lib/api/marketing.ts` | `getSocialConnections`, `connectMeta`, `verifySocialConnection`, `disconnectSocial`; campaign mapping incl. channels/media; config flags; `getCampaignPosts`, `publishCampaign`, `retryPost` |
| `src/lib/i18n/labels.ts` | `postStatusLabel` |
| `src/components/marketing/AiDraftPanel.tsx` | "Write with AI" section on the campaign form (dish/deal picker, notes, drafts left, unverified-numbers warning) |
| `src/pages/admin/menu/DealsPage.tsx` | "Campaign" button per deal → `/admin/marketing/new?product=<id>` |
| `src/lib/api/uploads.ts` | `uploadCampaignMedia(file, onProgress)` (XHR for progress) |
| `src/types/Marketing.ts` | `SocialConnection`, `SocialConnections`, `MediaType`, campaign channel/media fields, `CampaignPost`, `PostChannel`, `PostStatus`, `PostsResult` |
| `public/locales/{en,ar}/marketing.json` | `accounts.*`, `form.channels.*`, `form.post.*`, `form.media.*`, `form.problems.*`, `detail.*`, `postStatus.*`, `posts.*` |
| `public/locales/{en,ar}/errors.json` | All error codes below |

### Settings / env

| Setting | Purpose |
|---|---|
| `SOCIAL_TOKEN_ENCRYPTION_KEY` | Fernet key(s) for Page tokens. Blank = connecting is switched off (`error.socialEncryptionNotConfigured`) |
| `META_APP_SECRET` | Signs Graph calls (`appsecret_proof`). Optional unless the app requires it |
| `META_GRAPH_API_VERSION` | Default `v23.0`; set to the version shown in the Meta app |
| `CLOUDINARY_MAX_VIDEO_MB` | Campaign video cap, default 100 (Cloudinary free plan) |
| `AI_CAMPAIGN_DRAFTS_PER_DAY` | "Write with AI" allowance per branch per day, default 30 (separate from `AI_DRAFTS_PER_DAY`) |
| `META_APP_ID`, `META_OAUTH_REDIRECT_URI` | "Connect with Facebook" (M7). Both plus `META_APP_SECRET` required, else the button is hidden. The redirect URI is the admin's `/admin/marketing/accounts/facebook-callback`, registered exactly in the Meta app |

### How to run the tests

`.env` points at the shared Neon database — never run tests or `migrate` against it from a session; the user runs migrations. Use a temp SQLite:

```
DATABASE_URL="sqlite:///$(cygpath -m "$TEMP")/social_test.sqlite3" venv/Scripts/python manage.py test marketing common --noinput
```

Admin: `npx tsc -b --noEmit` and `npm run check:rtl` (use logical `ms-/me-/ps-/pe-/text-start` classes).

### Pending on the user's side

1. Generate and set `SOCIAL_TOKEN_ENCRYPTION_KEY` (command in `.env.example`), back it up.
2. `venv\Scripts\python manage.py migrate marketing` (0002 + 0003 + 0004).
3. Meta prerequisites (bottom of this file); then paste a Page ID + token on Social accounts to try M1 for real.
4. For M7: set `META_APP_ID`, `META_APP_SECRET`, `META_OAUTH_REDIRECT_URI`; add the same redirect URI under Facebook Login > Settings > Valid OAuth redirect URIs (Facebook Login product added to the app). Until App Review, only app roles/testers can complete the login.
5. Try for real: text/image/video post to Facebook; image + Reel to Instagram (check the padded image renders: open one `.../upload/if_ar_lt_0.8/.../x.jpg` URL); "Write with AI" in Arabic; the Facebook login.
6. Consider raising `META_GRAPH_API_VERSION` — Meta's docs were on v26.0 on 2026-10-21; our default is v23.0.

## Data model

### `SocialConnection` (built, M1)

| Field | Notes |
|---|---|
| `branch` | FK `Branch` |
| `provider` | `meta` only for now (FB + IG together — IG publishes with the Page token). `whatsapp` added when its Cloud API moves to per-restaurant |
| `status` | `connected` / `expired` (token rejected, code 190/102) / `error`, plus `last_error` |
| `page_id`, `page_name` | Facebook Page |
| `ig_user_id`, `ig_username` | Instagram professional account linked to the Page; blank = Instagram unavailable |
| `access_token_enc` | Fernet ciphertext of the **Page** token. Read only via `social.page_token(connection)` |
| `last_checked_at`, `connected_at`, `connected_by` | |

- Not a `BaseModel`: disconnect is a real delete, so no orphan encrypted token remains. Unique `(branch, provider)`.
- `connect_meta` accepts a Page token **or** a Page admin's user token (swapped for the Page token via `GET /{page_id}?fields=access_token`), and fails before calling Meta when there's no encryption key.

### `Campaign` (fields added in M2)

- `send_whatsapp` (default `True`, so pre-channel clients and old rows stay WhatsApp), `post_facebook`, `post_instagram`. The old `channel` field is removed.
- `post_caption` (≤ 2200): the public post text, separate from `message_en`/`message_ar` because `{{name}}` makes no sense in a public post. One field, any language.
- `media_type` (`image` / `video`) + `media_url`, both or neither. URL must pass `cloudinary.is_own_upload(url, media_type)`. No public id stored; Cloudinary transformations are built by inserting into the URL after `/upload/`.
- Rules in `CampaignSerializer.validate`: ≥ 1 channel; WhatsApp ⇒ message; Facebook ⇒ caption or media; Instagram ⇒ media and ≤ 30 hashtags.
- `prepare()` answers 409 `error.campaignNoWhatsapp` for a post-only campaign. A post-only campaign moves `draft` → `sent` once every ticked channel is posted (M3).

### `CampaignPost` (built, M3)

| Field | Notes |
|---|---|
| `campaign`, `channel` | `facebook` / `instagram`; unique `(campaign, channel)` |
| `status` | `pending` / `processing` (IG video only) / `posted` / `failed` |
| `container_id` | Instagram media container (M4) |
| `external_id`, `permalink` | The live post |
| `caption`, `media_url` | Snapshot of what was posted (same rule as `CampaignRecipient.message`) |
| `error`, `attempts`, `posted_at` | |

## Error codes

Built: `error.metaOauthNotConfigured`, `error.metaOauthExpired` (bad/expired/other person's state or session), `error.metaOauthFailed` (code exchange refused, Meta's message in `detail`) (M7), `error.aiDraftNeedsSubject`, `error.aiDraftProductInvalid` (M6; the AI call itself reuses `imports.aiNotConfigured` / `aiDailyLimit` / `aiTimeout` / `aiTooLong` / `aiFailed` / `aiTextTooLong`), `error.messageTooLongForMedia` (M5), `error.instagramNotConnected`, `error.instagramMediaRejected`, `error.instagramLimitReached` (M4, per-post), `error.metaNotConnected`, `error.campaignAlreadyPublished`, `error.campaignCancelled` (publish/retry on a cancelled campaign), `error.campaignNoPosts` (no publishable channel ticked), `error.postNotFailed` (retry on a post that isn't failed), `error.metaPageIdInvalid`, `error.metaTokenRequired`, `error.metaTokenInvalid`, `error.metaNotPageAdmin`, `error.metaRequestFailed` (with Meta's message in `detail`), `error.metaUnreachable` (502), `error.socialEncryptionNotConfigured`, `error.campaignChannelRequired`, `error.campaignMediaInvalid`, `error.facebookPostEmpty`, `error.instagramNeedsMedia`, `error.instagramTooManyHashtags`, `error.captionTooLong`, `error.campaignNoWhatsapp`.

Planned: none outstanding before M7.

Each new code needs en + ar entries in `errors.json`.

## Milestones

- [x] **M1** — `SocialConnection`, token encryption, paste form, Marketing → Social accounts page (`/admin/marketing/accounts`).
- [x] **M2** — Media upload (`campaign_image` / `campaign_video` signature kinds, `CLOUDINARY_MAX_VIDEO_MB`), campaign channel/caption/media fields and rules, tick boxes in the form, detail page preview. No posting yet.
- [x] **M3 — Facebook posting** (built 2026-10-21; notes below the list)
  1. `CampaignPost` model + migration 0004.
  2. `meta.graph_post(path, token, data)` with the same error handling as `graph_get`.
  3. New `marketing/publishing.py`: `publish_facebook(campaign, connection)`:
     - text only → `POST /{page_id}/feed` `{message}`
     - image → `POST /{page_id}/photos` `{url, caption}`
     - video → `POST /{page_id}/videos` `{file_url, description}`
     - then `GET /{id}?fields=permalink_url` for the link.
  4. `POST /api/campaigns/{id}/publish/`: lock the campaign row (`select_for_update`), skip channels whose post is already `posted`/`processing`, create/update `CampaignPost` rows, call Meta. A token error marks the connection `expired`. Responds with the posts. 409 `error.metaNotConnected` when there is no working connection.
  5. `GET /api/campaigns/{id}/posts/` and `POST /api/campaigns/{id}/posts/{channel}/retry/` (failed only).
  6. `Campaign.is_editable` also becomes false once any post is `posted`/`processing`. A post-only campaign moves to `sent` (with `sent_at`) when every ticked channel is posted; WhatsApp campaigns keep their recipient-driven status.
  7. Admin: "Publish" button on the detail page with a confirm ("posts publicly now"), per-channel status card (pill, permalink "View post", error + Retry). API functions + types + en/ar strings.
  8. Tests: mock `requests.post`; cover each FB variant, double publish, token expiry, retry, editing lock, post-only → `sent`.
  - *As built:* the publish lock is held across the Meta call (synchronous, by design). Post `error` stores Meta's own message for `error.metaRequestFailed`, otherwise the code; the admin translates codes and shows sentences as-is. A failed post leaves the campaign editable; unticking that channel hides its row (`posts` lists ticked channels only). A WhatsApp+Facebook campaign can be published at any status except `cancelled`; its status stays recipient-driven. Delete is refused once a post is live. A POST that times out (`error.metaUnreachable`) may still have posted — the admin warns to check the Page before Retry. Facebook video uses a 120 s timeout because Meta fetches `file_url` before answering.
- [x] **M4 — Instagram posting** (built 2026-10-21; notes below the list)
  1. Image: `POST /{ig_user_id}/media` `{image_url: <media_url with f_jpg>, caption}` → container id → `POST /{ig_user_id}/media_publish` `{creation_id}` → `GET /{media_id}?fields=permalink`.
  2. Video: `media_type=REELS, video_url, caption` → store `container_id`, status `processing`. `GET .../posts/` checks `GET /{container_id}?fields=status_code`: `FINISHED` → publish, `ERROR`/`EXPIRED` → failed, `IN_PROGRESS` → keep waiting. Admin polls every ~5 s while anything is processing.
  3. Aspect ratio: Instagram rejects images outside 4:5…1.91:1. Decide between padding via a Cloudinary transformation (`c_pad,ar_4:5,b_auto`) or failing with `error.instagramMediaRejected` — check against Meta docs when building.
  4. Optional: `GET /{ig_user_id}/content_publishing_limit` before publishing (100 posts / 24 h).
  5. Confirm current Reels limits (length, size, codec) against Meta docs.
  - *As built (checked against Meta's IG User Media reference, 2026-10-21):* images must be JPEG, 4:5…1.91:1, ≤ 1440 wide, ≤ 8 MB. Decision on item 3: **pad, don't fail** — Instagram's copy of an image is delivered via `cloudinary.transformed_url(..., INSTAGRAM_IMAGE_TRANSFORMATION, extension="jpg")` (conditional `c_pad,b_auto` to the nearest allowed ratio, `c_limit,w_1440`, `q_auto`, `.jpg` converts PNG/WebP). Facebook still gets the original. Reels: MP4/MOV, H.264/HEVC, 3 s–15 min, ≤ 300 MB (our 100 MB upload cap is below that), sent untransformed; a container that ends `ERROR`/`EXPIRED` fails the post with `error.instagramMediaRejected` (Meta's `status` sentence is logged, not stored). The limit endpoint is checked before each Instagram post (docs say 100 per 24 h in one place, 50 in another — we read `config.quota_total` instead of hard-coding); if the check itself fails, publishing goes ahead.
  - Every image goes through `processing` too if Meta isn't done at the first status check; usually it's `FINISHED` at once and publishes in the same request. A failed *status check* (network) leaves the post `processing`; a `PUBLISHED` container (an earlier request timed out after Meta acted) is recorded as posted without a media id. Retry builds a fresh container. A Page with no Instagram account fails only the Instagram post (`error.instagramNotConnected`), not the whole publish.
- [x] **M5 — WhatsApp media link**: when a campaign has media, `marketing/services.py` appends `media_url` to each rendered recipient message (and the preview), so the `wa.me` text carries a link WhatsApp previews. Watch the 1000-char message limit.
  - *As built:* `services.with_media_link(text, media_url)` adds `"\n\n" + url` after placeholders are filled; used by `preview_message` and `build_recipients`. The preview endpoint takes an optional `media_url` (display only, not validated). `CampaignSerializer.validate` refuses a WhatsApp message whose length + link exceeds 1000 (`error.messageTooLongForMedia`, per language field). Admin: form shows the link length and remaining room under the message and blocks save with the same rule; detail page shows the link under the WhatsApp message. Tests: `WhatsappMediaLinkTests`.
- [x] **M6 — AI captions (NVIDIA)** (built 2026-10-21; notes below the list)
  1. Read `imports/ai.py` first; reuse its client, timeout and error handling.
  2. `POST /api/campaigns/ai-draft/` (detail=False, works for an unsaved form) `{deal_id? | product_id?, notes, channels}` → `{post_caption, message_en, message_ar}`. Nothing is saved.
  3. Separate daily quota `AI_CAMPAIGN_DRAFTS_PER_DAY` (see how `AI_DRAFTS_PER_DAY` is counted in imports).
  4. Admin: "Write with AI" button on the campaign form fills the fields for editing; "Create campaign" on a deal (`/admin/menu/deals`) opens the form with `?deal=<id>`.
  5. Check Arabic quality; model is switchable via `AI_MODEL`.
  - *As built:* the endpoint takes `product_id` rather than `deal_id`: a deal is one per product (`Deal.product` is OneToOne) and the admin's deals list is keyed by product, so the product brings its deal (price + windows) with it. Context sent: restaurant names, product names/descriptions/category/price/active sizes, deal; never cost price. One call, reasoning off (`AI_FAST_EXTRA_BODY`), temperature 0.7, 120 s timeout. Only the ticked channels' texts are written. Numbers in the draft that appear in neither the data nor the notes (Arabic digits normalised, hashtags ignored) come back as `unverified_numbers`, shown as a warning on the form. Usage logged in `CampaignAiDraft` before the call (failures count). Config gains `ai_enabled`, `ai_drafts_left_today`. The form asks before replacing text already typed. Tests: `CampaignAiDraftTests` (patch `imports.ai.ask`).
- [x] **M7 — "Connect with Facebook" (OAuth)** (built 2026-10-21; notes below): add `META_APP_ID`; backend issues the Facebook Login dialog URL with a signed `state`; admin callback route posts `code` back; backend exchanges it for a user token, then a long-lived token (`fb_exchange_token`), lists `/me/accounts`, the owner picks a Page, and it is saved through `social.connect_meta`. Redirect URI must be registered in the Meta app. Needs App Review for other restaurants' Pages.
  - *As built:* `POST .../meta/oauth/start/` → dialog URL with a `signing`-signed `state` (owner + branch + nonce, 15 min). The admin callback posts `{code, state}` to `.../meta/oauth/pages/` (owner only): code → short token → long-lived token (`fb_exchange_token`) → `/me/accounts` (id, name, tasks, Instagram). The user token goes back to the browser only as a signed `session` holding the Fernet-encrypted token (15 min, bound to the owner), never stored server-side. `.../meta/oauth/connect/ {session, page_id}` calls `connect_meta` with that user token, which reads the (non-expiring) Page token. Scopes: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `business_management` (Pages in a Business portfolio), `instagram_basic`, `instagram_content_publish`. Pages where the person lacks `CREATE_CONTENT`/`MANAGE` are listed but not selectable. The callback page exchanges the code once (ref guard for StrictMode) and strips it from the URL. Tests: `MetaOAuthTests`.
- [ ] **Later** — scheduling (`scheduled_for` exists; FB supports `scheduled_publish_time`, IG needs our own job), `Restaurant` model, WhatsApp Cloud API / Embedded Signup per restaurant, post insights (likes/reach).

## Meta prerequisites (run in parallel with the code)

1. Create a Meta Developer App (type Business) with `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`, `instagram_basic`, `instagram_content_publish`.
2. Complete Business Verification for the company.
3. App Review needs a privacy policy URL, a data deletion URL and a screencast — can take weeks, so start now.
4. For testing: a test Facebook Page with a linked Instagram professional account, and testers added to the app. A token for testing comes from Graph API Explorer (extend it with the Access Token Debugger).
