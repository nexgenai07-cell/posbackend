from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CampaignViewSet, MarketingConfigView, SocialConnectionViewSet

router = DefaultRouter()
router.register("campaigns", CampaignViewSet, basename="campaign")
router.register("marketing/connections", SocialConnectionViewSet, basename="social-connection")

urlpatterns = [
    path("marketing/config/", MarketingConfigView.as_view(), name="marketing-config"),
] + router.urls
