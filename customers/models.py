from django.db import models

from branches.models import Branch
from common.models import BaseModel


class MarketingConsent(models.TextChoices):
    """
    Three states, not a boolean, because "we never asked" and "they said no"
    are different permissions, and collapsing them into False would make an
    unasked customer permanently unreachable the moment opt-in tracking
    started. Campaign audiences choose which states they accept
    (marketing/models.py's AudienceConsent): WhatsApp's Business Policy wants
    OPTED_IN before a template goes out over the Cloud API, while a manual
    send from the owner's own phone can reasonably include UNKNOWN. OPTED_OUT
    is never targetable by either.
    """

    UNKNOWN = "unknown", "Never asked"
    OPTED_IN = "opted_in", "Opted in"
    OPTED_OUT = "opted_out", "Opted out"


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
    marketing_consent = models.CharField(
        max_length=10, choices=MarketingConsent.choices, default=MarketingConsent.UNKNOWN
    )
    # When the state last changed, kept for the "prove you had consent"
    # question PDPL asks after the fact. Null while consent is UNKNOWN.
    consent_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "phone"], name="unique_phone_per_branch"),
        ]

    def __str__(self):
        return self.name or self.phone
