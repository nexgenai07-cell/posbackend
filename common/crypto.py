"""
Encryption at rest for secrets kept in the database.

Each kind of secret has its own key in its own environment variable, so
losing or rotating one never touches the others: ZATCA device keys use
ZATCA_ENCRYPTION_KEY (zatca/crypto.py), social-media page tokens use
SOCIAL_TOKEN_ENCRYPTION_KEY (marketing/social.py).

A key is a Fernet key, or several separated by commas to rotate: the first
encrypts, every one of them can still decrypt (MultiFernet). Keys are never
generated automatically: losing one means re-entering every secret it
protected, so it must be created once on purpose and backed up apart from
the database.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class EncryptionNotConfigured(Exception):
    """The key setting is missing or not a valid Fernet key."""


class EncryptionKeyMismatch(Exception):
    """A stored secret can't be decrypted with any configured key."""


class SecretBox:
    """Encrypts and decrypts with the key held in one named setting."""

    def __init__(self, setting, label):
        self.setting = setting
        # How a failed decrypt describes the secret, e.g. "A ZATCA secret".
        self.label = label

    def _fernet(self):
        # Read on every call rather than cached, so override_settings in
        # tests and a key added to a running process both just work.
        raw = getattr(settings, self.setting, "") or ""
        keys = [key.strip() for key in raw.split(",") if key.strip()]
        if not keys:
            raise EncryptionNotConfigured(f"{self.setting} is not set")
        try:
            return MultiFernet([Fernet(key) for key in keys])
        except (ValueError, TypeError):
            # Never echo the value: it is (meant to be) a secret.
            raise EncryptionNotConfigured(f"{self.setting} is not a valid Fernet key") from None

    def status(self):
        """(configured, problem) for a status page — no key material either way."""
        try:
            self._fernet()
        except EncryptionNotConfigured as exc:
            return False, "missing" if "not set" in str(exc) else "invalid"
        return True, ""

    def encrypt(self, plaintext):
        if not plaintext:
            return ""
        return self._fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token):
        if not token:
            return ""
        try:
            return self._fernet().decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise EncryptionKeyMismatch(f"{self.label} can't be decrypted with the configured key") from None
