from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.models import Staff
from branches.models import Branch
from common.models import BaseModel
from customers.models import Customer
from orders.models import Bill


class ReviewSource(models.TextChoices):
    # Left by a guest from the QR pay screen once their bill was settled.
    GUEST = "guest", "Guest (QR)"
    # Typed in by an owner/manager; shown on the website as a "Local Guide".
    ADMIN = "admin", "Added by admin"


class ReviewStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class Review(BaseModel):
    """
    A restaurant review shown on the customer website once approved.

    Guest reviews are tied to the Bill they were left for — one per bill, and
    only once that bill is paid with everything on it served (reviews/services.py).
    They always start `pending`; nothing a guest writes reaches the website
    until an owner/manager approves it. Admin-added reviews are published
    straight away.
    """

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="reviews")
    # Unique: a bill can be reviewed once. Still unique for a soft-deleted
    # review, so deleting a guest's review never lets them post another.
    bill = models.OneToOneField(Bill, on_delete=models.SET_NULL, null=True, blank=True, related_name="review")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviews")
    name = models.CharField(max_length=80)
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True, max_length=1000)
    source = models.CharField(max_length=8, choices=ReviewSource.choices, default=ReviewSource.GUEST)
    status = models.CharField(max_length=10, choices=ReviewStatus.choices, default=ReviewStatus.PENDING)
    # The date shown on the website. Admins may backdate a review they copy in.
    posted_at = models.DateTimeField(default=timezone.now)
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="moderated_reviews"
    )
    created_by = models.ForeignKey(
        Staff, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_reviews"
    )

    class Meta:
        ordering = ["-posted_at", "-id"]
        indexes = [models.Index(fields=["branch", "status", "-posted_at"])]

    def __str__(self):
        return f"Review #{self.pk} {self.rating}★ ({self.status})"
