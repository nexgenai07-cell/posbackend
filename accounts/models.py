from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from branches.models import Branch
from common.models import BaseModel

from .usernames import normalize_username, unique_username


class StaffRole(models.TextChoices):
    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"
    CASHIER = "cashier", "Cashier"
    WAITER = "waiter", "Waiter"
    KITCHEN = "kitchen", "Kitchen"


class Staff(BaseModel):
    """
    Not Django's AUTH_USER_MODEL. Login is username + password + JWT through
    a custom auth flow (accounts/services.py, accounts/authentication.py),
    not Django's session system, so there's no need for AbstractBaseUser's
    create_user/is_staff/USERNAME_FIELD machinery here. Django admin login
    (for developers) stays on the default auth.User, kept entirely separate
    from restaurant staff records.

    Lockout: after MAX_FAILED_LOGINS wrong passwords in a row the row is
    locked (`locked_at` set). A locked account refuses even the right
    password and its existing tokens stop working; only an Owner (or
    `manage.py unlock_staff`) clears it. "Locked" is deliberately separate
    from `is_active`: inactive means the person left, locked means a
    security block, and an unlock must never re-activate a leaver.
    """

    MAX_FAILED_LOGINS = 3
    LOCK_REASON_FAILED_LOGINS = "failed_logins"

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="staff")
    name = models.CharField(max_length=255)
    # What the staff member types to sign in. Stored lowercase; unique across
    # every branch (login has no branch picker) and across soft-deleted rows,
    # so a removed account's name is never silently handed to a new hire.
    username = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=16, choices=StaffRole.choices)
    # Empty means "cannot log in" - e.g. an account created without a password.
    password_hash = models.CharField(max_length=128, blank=True, default="")
    # Set whenever someone *else* chose the password (migration from PINs, a
    # new account, an owner's reset). The token then only works for
    # change-password/logout/me until the staff member picks their own.
    must_change_password = models.BooleanField(default=False)
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_reason = models.CharField(max_length=32, blank=True)
    unlocked_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    unlocked_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    shift_template = models.ForeignKey(
        "accounts.ShiftTemplate", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_staff",
    )
    # The kitchen stations a kitchen chef works at; their screen shows these
    # stations' lines plus General. Ignored for every other role.
    stations = models.ManyToManyField("kitchen.KitchenStation", blank=True, related_name="chefs")
    # Head chef / expo: a kitchen chef who sees every station by default,
    # moves lines between stations and hands finished orders to the waiter.
    # Owners and managers can always do all of this. Ignored for other roles.
    is_head_chef = models.BooleanField(default=False)

    # Not a Django auth user, but DRF's permission plumbing (and anything
    # checking request.user) expects these two attributes to exist.
    is_authenticated = True
    is_anonymous = False

    def save(self, *args, **kwargs):
        # A row saved without a username gets one from its name, the same
        # rule the migration applied to existing staff. The owner can edit it.
        self.username = normalize_username(self.username)
        if not self.username:
            self.username = unique_username(
                self.name,
                lambda candidate: Staff.all_objects.filter(username=candidate).exclude(pk=self.pk).exists(),
            )
        super().save(*args, **kwargs)

    # -- credentials ---------------------------------------------------------

    def set_password(self, raw_password):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        if not self.password_hash:
            return False
        return check_password(raw_password, self.password_hash)

    # -- lockout -------------------------------------------------------------

    @property
    def is_locked(self):
        return self.locked_at is not None

    def lock(self, reason):
        self.locked_at = timezone.now()
        self.locked_reason = reason

    def unlock(self, by=None):
        """Clears the lock and the counter and records who did it. Does not
        touch `is_active` - see the class docstring."""
        self.locked_at = None
        self.locked_reason = ""
        self.failed_login_attempts = 0
        self.unlocked_by = by
        self.unlocked_at = timezone.now()

    def __str__(self):
        return f"{self.name} ({self.role})"


class Shift(BaseModel):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="shifts")
    clock_in = models.DateTimeField()
    clock_out = models.DateTimeField(null=True, blank=True)
    is_late = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        indexes = [
            # Attendance: one staff member's shifts between two dates.
            models.Index(fields=["staff", "clock_in"], name="shift_staff_clock_in"),
        ]

    def __str__(self):
        return f"{self.staff.name} shift starting {self.clock_in:%Y-%m-%d %H:%M}"


class ShiftTemplate(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="shift_templates")
    name = models.CharField(max_length=120)
    start_time = models.TimeField()
    end_time = models.TimeField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["start_time", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "name"], condition=models.Q(is_deleted=False),
                name="unique_shift_template_name_per_branch",
            ),
        ]

    @property
    def crosses_midnight(self):
        return self.end_time < self.start_time

    def clean(self):
        super().clean()
        if self.start_time == self.end_time:
            raise ValidationError({"end_time": "A shift cannot start and end at the same time."})

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M}–{self.end_time:%H:%M})"
