from django.contrib.auth.hashers import check_password, make_password
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

    def __str__(self):
        return f"{self.staff.name} shift starting {self.clock_in:%Y-%m-%d %H:%M}"
