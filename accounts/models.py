from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import models

from branches.models import Branch
from common.models import BaseModel


class StaffRole(models.TextChoices):
    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"
    CASHIER = "cashier", "Cashier"
    WAITER = "waiter", "Waiter"
    KITCHEN = "kitchen", "Kitchen"


class Staff(BaseModel):
    """
    Not Django's AUTH_USER_MODEL. Login is PIN + JWT through a custom auth
    flow, not Django's username/password system, so there's no need for
    AbstractBaseUser's create_user/is_staff/USERNAME_FIELD machinery here.
    Django admin login (for developers) stays on the default auth.User,
    kept entirely separate from restaurant staff records.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="staff")
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=StaffRole.choices)
    pin_hash = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    shift_template = models.ForeignKey(
        "accounts.ShiftTemplate", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_staff",
    )

    # Not a Django auth user, but DRF's permission plumbing (and anything
    # checking request.user) expects these two attributes to exist.
    is_authenticated = True
    is_anonymous = False

    def set_pin(self, raw_pin):
        self.pin_hash = make_password(raw_pin)

    def check_pin(self, raw_pin):
        return check_password(raw_pin, self.pin_hash)

    @classmethod
    def pin_taken_in_branch(cls, branch, raw_pin, exclude_pk=None):
        """
        PINs are hashed with a random salt, so uniqueness can't be a DB
        constraint on pin_hash — checked here instead, called from the
        staff create/update serializer before saving.
        """
        qs = cls.objects.filter(branch=branch)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return any(staff.check_pin(raw_pin) for staff in qs)

    def __str__(self):
        return f"{self.name} ({self.role})"


class Shift(BaseModel):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="shifts")
    clock_in = models.DateTimeField()
    clock_out = models.DateTimeField(null=True, blank=True)
    is_late = models.BooleanField(default=False)

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
