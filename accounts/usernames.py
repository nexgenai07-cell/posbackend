"""
Username rules, shared by the Staff model, the serializer and the data
migration that gives existing staff their first username.

A username is what a staff member types on the login screen instead of
picking their name from a list, so it has to be short, typeable on a POS
keyboard and unambiguous: lowercase ASCII letters, digits, dot, underscore
and hyphen, 3-64 characters. "Ali Khan" becomes ``ali.khan``; a second Ali
Khan becomes ``ali.khan2``.
"""

import re
import unicodedata

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

# What a name made only of characters we can't transliterate (e.g. an Arabic
# name) falls back to. The owner is expected to rename it on the Staff page.
FALLBACK_USERNAME = "staff"


def normalize_username(value):
    """What is stored and compared: trimmed and lowercased."""
    return (value or "").strip().lower()


def is_valid_username(value):
    return bool(USERNAME_PATTERN.match(value or ""))


def username_from_name(name):
    """
    Best-effort ASCII slug of a display name: accents stripped, spaces turned
    into dots, anything else dropped. Never empty - see FALLBACK_USERNAME.
    """
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"\s+", ".", ascii_name.strip().lower())
    slug = re.sub(r"[^a-z0-9._-]", "", slug)
    slug = re.sub(r"\.{2,}", ".", slug).strip("._-")
    if len(slug) < USERNAME_MIN_LENGTH:
        slug = FALLBACK_USERNAME if not slug else slug + FALLBACK_USERNAME
    return slug[:USERNAME_MAX_LENGTH]


def unique_username(name, taken):
    """
    `taken` is a callable: taken(candidate) -> bool. Tries the plain slug
    first, then slug2, slug3, ... so the common case stays readable.
    """
    base = username_from_name(name)
    candidate = base
    suffix = 2
    while taken(candidate):
        tail = str(suffix)
        candidate = base[: USERNAME_MAX_LENGTH - len(tail)] + tail
        suffix += 1
    return candidate
