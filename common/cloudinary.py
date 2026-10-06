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


class CloudinaryNotConfigured(RuntimeError):
    """One of the CLOUDINARY_* settings is blank — uploads are switched off."""


def is_configured():
    return bool(
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    )


def build_signature(folder=UPLOAD_FOLDER, timestamp=None):
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
        "upload_url": f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/upload",
        "max_bytes": MAX_UPLOAD_BYTES,
        "allowed_types": list(ALLOWED_IMAGE_TYPES),
    }
