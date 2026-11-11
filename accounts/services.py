"""
The login state machine and the owner-side account operations (unlock,
reset password). Views, the Django admin and the unlock_staff management
command all go through here so the lockout rules live in exactly one place.
"""

import logging
import uuid
from functools import lru_cache

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework import status

from realtime.publisher import publish_staff_event

from .models import Staff, StaffRole
from .usernames import normalize_username

# Every lock/unlock and every refused login lands here; wire a handler in
# LOGGING to keep the history somewhere durable.
logger = logging.getLogger("accounts.auth")


class LoginRejected(Exception):
    """Why a login was refused. `attempts_left` is only set for a wrong
    password on an account that is not (yet) locked."""

    def __init__(self, code, status_code, attempts_left=None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.attempts_left = attempts_left


@lru_cache(maxsize=1)
def _dummy_hash():
    # Hashed once, lazily: used to spend the same time on an unknown username
    # as on a wrong password, so response timing doesn't reveal which
    # usernames exist.
    return make_password("not-a-real-password")


def authenticate_staff(username, password):
    """
    Returns the Staff on success, raises LoginRejected otherwise.

    The whole check runs under a row lock (select_for_update) so that
    parallel wrong attempts can't each read "2 failures" and all write "3":
    the counter is read, incremented and written by one request at a time.
    """
    # The rejection is *returned* from the locked section and raised only
    # after the transaction has committed: raising inside atomic() would roll
    # back the very counter increment / lock the rejection reports.
    with transaction.atomic():
        staff, rejection = _attempt(normalize_username(username), password)
    if rejection is not None:
        raise rejection
    return staff


def _attempt(username, password):
    """Runs inside authenticate_staff()'s transaction. Returns
    (staff, None) on success or (staff_or_None, LoginRejected)."""
    staff = Staff.objects.select_for_update().filter(username=username, is_active=True).first()
    if staff is None:
        check_password(password, _dummy_hash())
        logger.info("Login refused: unknown or inactive username %r", username)
        return None, LoginRejected("error.invalidCredentials", status.HTTP_400_BAD_REQUEST)

    if staff.is_locked:
        logger.info("Login refused: %s (#%s) is locked", staff.username, staff.pk)
        return staff, LoginRejected("error.accountLocked", status.HTTP_423_LOCKED)

    if not staff.check_password(password):
        staff.failed_login_attempts += 1
        if staff.failed_login_attempts >= Staff.MAX_FAILED_LOGINS:
            staff.lock(Staff.LOCK_REASON_FAILED_LOGINS)
            staff.save(update_fields=["failed_login_attempts", "locked_at", "locked_reason", "updated_at"])
            logger.warning(
                "Account locked: %s (#%s) after %s failed logins",
                staff.username, staff.pk, staff.failed_login_attempts,
            )
            notify_owners_of_lock(staff)
            return staff, LoginRejected("error.accountLocked", status.HTTP_423_LOCKED)
        staff.save(update_fields=["failed_login_attempts", "updated_at"])
        attempts_left = Staff.MAX_FAILED_LOGINS - staff.failed_login_attempts
        logger.info("Login refused: wrong password for %s (#%s), %s left", staff.username, staff.pk, attempts_left)
        return staff, LoginRejected("error.invalidCredentials", status.HTTP_400_BAD_REQUEST, attempts_left=attempts_left)

    if staff.failed_login_attempts:
        staff.failed_login_attempts = 0
        staff.save(update_fields=["failed_login_attempts", "updated_at"])
    return staff, None


def notify_owners_of_lock(staff):
    """
    Realtime "X's account was locked" to every active Owner of the branch,
    through the staff-targeted Channels groups (realtime/publisher.py). The
    message is delivered on commit, so it never announces a lock that was
    rolled back. Note the key is `locked_staff_id`, not `staff_id`: the admin
    app treats `staff_id` as "the intended recipient" and would drop it.
    """
    owner_ids = Staff.objects.filter(
        branch_id=staff.branch_id, role=StaffRole.OWNER, is_active=True,
    ).values_list("pk", flat=True)
    payload = {
        "id": staff.pk,
        "locked_staff_id": staff.pk,
        "staff_name": staff.name,
        "username": staff.username,
        "locked_at": staff.locked_at.isoformat() if staff.locked_at else None,
        "event_id": uuid.uuid4().hex,
    }
    for owner_id in owner_ids:
        publish_staff_event(owner_id, "staff:locked", payload)


def unlock_staff(staff, by=None, new_password=None):
    """Owner (or developer, by=None) clears a lock. An optional temporary
    password also forces a change on the next login."""
    staff.unlock(by=by)
    fields = ["locked_at", "locked_reason", "failed_login_attempts", "unlocked_by", "unlocked_at", "updated_at"]
    if new_password:
        staff.set_password(new_password)
        staff.must_change_password = True
        fields += ["password_hash", "must_change_password"]
    staff.save(update_fields=fields)
    logger.warning(
        "Account unlocked: %s (#%s) by %s%s",
        staff.username, staff.pk,
        f"{by.username} (#{by.pk})" if by else "management command",
        " with a temporary password" if new_password else "",
    )
    return staff


def reset_password(staff, by, new_password):
    """Owner sets a temporary password for someone who forgot theirs. Leaves
    any lock in place - unlocking is a separate, explicit decision."""
    staff.set_password(new_password)
    staff.must_change_password = True
    staff.save(update_fields=["password_hash", "must_change_password", "updated_at"])
    logger.warning("Password reset: %s (#%s) by %s (#%s)", staff.username, staff.pk, by.username, by.pk)
    return staff


def change_own_password(staff, new_password):
    staff.set_password(new_password)
    staff.must_change_password = False
    staff.failed_login_attempts = 0
    staff.save(update_fields=["password_hash", "must_change_password", "failed_login_attempts", "updated_at"])
    logger.info("Password changed: %s (#%s) at %s", staff.username, staff.pk, timezone.now().isoformat())
    return staff
