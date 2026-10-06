import secrets

from django.db import models

from branches.models import Branch
from common.models import BaseModel


class TableStatus(models.TextChoices):
    EMPTY = "empty", "Empty"
    OCCUPIED = "occupied", "Occupied"
    NEEDS_BILL = "needs-bill", "Needs Bill"


class Table(BaseModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="tables")
    label_en = models.CharField(max_length=100)
    label_ar = models.CharField(max_length=100)
    # How many guests this table seats. Configurable per restaurant rather than
    # assumed — staff answer "we are 8 people, do you have a table?" from this.
    # Default 2 is the commonest small-table size and keeps existing rows valid
    # without forcing a value into every historical table on migration.
    seats = models.PositiveSmallIntegerField(default=2)
    qr_code = models.CharField(max_length=255, blank=True)
    # Stable per-table token used by its permanent QR code.
    session_token = models.CharField(max_length=64, null=True, blank=True, unique=True)
    status = models.CharField(max_length=16, choices=TableStatus.choices, default=TableStatus.EMPTY)

    def __str__(self):
        return self.label_en

    def save(self, *args, **kwargs):
        # The printed QR is permanent per table. Occupancy is tracked separately
        # so creating a table never marks it occupied.
        if not self.session_token:
            self.session_token = secrets.token_urlsafe(24)
        if not self.qr_code:
            self.qr_code = f"/t/{self.session_token}"
        super().save(*args, **kwargs)

    def open(self):
        self.status = TableStatus.OCCUPIED
        self.save(update_fields=["session_token", "qr_code", "status", "updated_at"])

    def close(self):
        self.status = TableStatus.EMPTY
        self.save(update_fields=["status", "updated_at"])

    def mark_needs_bill(self):
        self.status = TableStatus.NEEDS_BILL
        self.save(update_fields=["status", "updated_at"])
