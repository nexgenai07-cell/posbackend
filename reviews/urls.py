from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import GuestReviewCreateView, GuestReviewEligibilityView, PublicReviewListView, ReviewViewSet

router = DefaultRouter()
router.register("reviews", ReviewViewSet, basename="review")

# Explicit paths first, so "qr"/"public" are never read as a review id.
urlpatterns = [
    path("reviews/qr/", GuestReviewCreateView.as_view(), name="review-qr-create"),
    path("reviews/qr/eligibility/", GuestReviewEligibilityView.as_view(), name="review-qr-eligibility"),
    path("reviews/public/", PublicReviewListView.as_view(), name="review-public-list"),
] + router.urls
