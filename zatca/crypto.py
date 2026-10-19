"""
Encryption at rest for ZATCA secrets (private keys, CSID tokens and secrets).

The key comes only from the ZATCA_ENCRYPTION_KEY environment variable — a
Fernet key, or several separated by commas to rotate: the first encrypts,
every one of them can still decrypt (MultiFernet). It is never generated
automatically: losing it means onboarding every device again, so it must be
created once on purpose and backed up apart from the database.

Ciphertext is decrypted only at the moment a secret is used, never when a
device row is merely loaded, so a missing or wrong key cannot break the
status pages — only the actions that actually need the secret.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class EncryptionNotConfigured(Exception):
    """ZATCA_ENCRYPTION_KEY is missing or not a valid Fernet key."""


class EncryptionKeyMismatch(Exception):
    """A stored secret can't be decrypted with any configured key."""


def _fernet():
    raw = getattr(settings, "ZATCA_ENCRYPTION_KEY", "") or ""
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if not keys:
        raise EncryptionNotConfigured("ZATCA_ENCRYPTION_KEY is not set")
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except (ValueError, TypeError):
        # Never echo the value: it is (meant to be) a secret.
        raise EncryptionNotConfigured("ZATCA_ENCRYPTION_KEY is not a valid Fernet key") from None


def encryption_status():
    """(configured, problem) for the status page — no key material either way."""
    try:
        _fernet()
    except EncryptionNotConfigured as exc:
        return False, "missing" if "not set" in str(exc) else "invalid"
    return True, ""


def encrypt(plaintext):
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise EncryptionKeyMismatch("A ZATCA secret can't be decrypted with the configured key") from None
