"""
Cloudinary signed direct uploads.

The only module that knows CLOUDINARY_API_SECRET exists — same containment rule
as orders/moyasar.py and the Moyasar secret key.

Why signed direct upload rather than proxying the bytes through Django:

  browser --(1) ask for signature--> Django   (secret stays here)
  browser --(2) POST file + signature--> Cloudinary
  browser <--(3) secure_url---------- Cloudinary
  browser --(4) save that URL-------> Django

The image never passes through our server, so a 5 MB upload does not occupy a
Django worker, and the API secret never reaches the browser. The signature is
what proves to Cloudinary that WE authorised this upload: it is an SHA-1 of the
upload parameters plus the secret, so a client cannot alter the folder, the
timestamp or anything else we pinned without invalidating it.

Nothing here uploads anything. Django only ever signs.
"""

import hashlib
import logging
import time

from django.conf import settings

logger = logging.getLogger("common.cloudinary")

# Everything this system uploads lands under one folder, so a Cloudinary
# account shared with other projects stays tidy and is easy to purge.
UPLOAD_FOLDER = "restaurant"

# Cloudinary's own cap for unsigned/basic plans is 10 MB; we mirror it here so
# the user is told "too large" before spending the upload, not after.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml")

# Supplier tax invoices / credit notes (PDF), stored as Cloudinary "raw"
# files in their own folder: never transformed, never shown as images.
DOCUMENT_FOLDER = f"{UPLOAD_FOLDER}/supplier-documents"
ALLOWED_DOCUMENT_TYPES = ("application/pdf",)

# Campaign media, posted to Facebook and Instagram. Narrower than product
# images: neither network takes SVG, and a GIF would post as a still. Video
# is MP4/MOV, the formats both accept. Cloudinary's free plan caps a video
# upload at 100 MB; a paid plan allows more, hence the setting.
CAMPAIGN_FOLDER = f"{UPLOAD_FOLDER}/campaigns"
CAMPAIGN_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
CAMPAIGN_VIDEO_TYPES = ("video/mp4", "video/quicktime")

# kind -> (folder, Cloudinary resource type, size cap, allowed MIME types).
# The folder is pinned per kind, so a signature for one can't be used to
# drop a file in another's folder.
KINDS = {
    "image": (UPLOAD_FOLDER, "image", lambda: MAX_UPLOAD_BYTES, ALLOWED_IMAGE_TYPES),
    "document": (DOCUMENT_FOLDER, "raw", lambda: MAX_UPLOAD_BYTES, ALLOWED_DOCUMENT_TYPES),
    "campaign_image": (CAMPAIGN_FOLDER, "image", lambda: MAX_UPLOAD_BYTES, CAMPAIGN_IMAGE_TYPES),
    "campaign_video": (
        CAMPAIGN_FOLDER, "video", lambda: settings.CLOUDINARY_MAX_VIDEO_MB * 1024 * 1024, CAMPAIGN_VIDEO_TYPES,
    ),
}


class CloudinaryNotConfigured(RuntimeError):
    """One of the CLOUDINARY_* settings is blank — uploads are switched off."""


def is_configured():
    return bool(
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    )


def is_own_upload(url, resource_type):
    """
    Whether `url` is a file in this restaurant's Cloudinary account, of the
    given resource type. Campaign media is posted to Facebook and Instagram
    exactly as stored, so a URL pointing anywhere else is refused rather
    than published under the restaurant's name.
    """
    if not settings.CLOUDINARY_CLOUD_NAME:
        return False
    prefix = f"https://res.cloudinary.com/{settings.CLOUDINARY_CLOUD_NAME}/{resource_type}/upload/"
    return url.startswith(prefix) and len(url) > len(prefix)


def transformed_url(url, transformation, extension=None):
    """
    A delivery URL for one of our uploads with a Cloudinary transformation
    applied, e.g. ".../image/upload/c_limit,w_1440/v1/x.jpg". Cloudinary
    builds the derived file on first request; nothing is stored here.

    `extension` swaps the file's extension, which is how Cloudinary is asked
    for a different format (".jpg" delivers a JPEG whatever was uploaded).
    """
    head, marker, tail = url.partition("/upload/")
    if not marker:
        return url
    if extension:
        stem, dot, _ = tail.rpartition(".")
        tail = f"{stem if dot else tail}.{extension.lstrip('.')}"
    return f"{head}/upload/{transformation}/{tail}"


def build_signature(folder=UPLOAD_FOLDER, timestamp=None, *, document=False, kind="image"):
    """
    Everything the browser needs to upload one file, and nothing more.

    Cloudinary's rule: sort the signed params alphabetically, join as
    `k=v&k=v`, append the API secret, SHA-1 the result. Any param the browser
    sends that is NOT in this dict is unsigned and would be rejected, which is
    precisely what stops a client uploading wherever it likes.

    The timestamp makes each signature short-lived (Cloudinary rejects stale
    ones), so a leaked signature cannot be replayed indefinitely.
    """
    if not is_configured():
        raise CloudinaryNotConfigured("CLOUDINARY_* settings are not set")

    if document:
        kind = "document"
    kind_folder, resource_type, max_bytes, allowed_types = KINDS[kind]
    if kind != "image":
        folder = kind_folder
    timestamp = timestamp or int(time.time())
    signed_params = {"folder": folder, "timestamp": timestamp}

    to_sign = "&".join(f"{key}={signed_params[key]}" for key in sorted(signed_params))
    signature = hashlib.sha1(f"{to_sign}{settings.CLOUDINARY_API_SECRET}".encode()).hexdigest()

    return {
        "signature": signature,
        "timestamp": timestamp,
        "folder": folder,
        # Safe to expose: the cloud name is in every delivered image URL, and
        # the API key is public by design — only the SECRET must stay here.
        "api_key": settings.CLOUDINARY_API_KEY,
        "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
        "upload_url": f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/{resource_type}/upload",
        "max_bytes": max_bytes(),
        "allowed_types": list(allowed_types),
    }
