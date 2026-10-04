from django.db import models

from branches.models import Branch
from common.models import BaseModel


class Customer(BaseModel):
    """
    Model only for now — Order.customer needs somewhere to point to.
    Endpoints (find-or-create by phone, etc.) are Phase 13's concern, once
    the public QR-ordering flow that actually creates customers is built.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="customers")
    phone = models.CharField(max_length=32)
    name = models.CharField(max_length=255, blank=True)
    last_order_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "phone"], name="unique_phone_per_branch"),
        ]

    def __str__(self):
        return self.name or self.phone
