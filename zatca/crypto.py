"""
Encryption at rest for ZATCA secrets (private keys, CSID tokens and secrets).

The key comes only from the ZATCA_ENCRYPTION_KEY environment variable — a
Fernet key, or several separated by commas to rotate (see common/crypto.py).
Losing it means onboarding every device again, so it must be created once on
purpose and backed up apart from the database.

Ciphertext is decrypted only at the moment a secret is used, never when a
device row is merely loaded, so a missing or wrong key cannot break the
status pages — only the actions that actually need the secret.
"""

from common.crypto import EncryptionKeyMismatch, EncryptionNotConfigured, SecretBox

__all__ = ["EncryptionKeyMismatch", "EncryptionNotConfigured", "decrypt", "encrypt", "encryption_status"]

_box = SecretBox("ZATCA_ENCRYPTION_KEY", label="A ZATCA secret")


def encryption_status():
    """(configured, problem) for the status page — no key material either way."""
    return _box.status()


def encrypt(plaintext):
    return _box.encrypt(plaintext)


def decrypt(token):
    return _box.decrypt(token)
