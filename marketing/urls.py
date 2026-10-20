from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CampaignViewSet, MarketingConfigView

router = DefaultRouter()
router.register("campaigns", CampaignViewSet, basename="campaign")

urlpatterns = [
    path("marketing/config/", MarketingConfigView.as_view(), name="marketing-config"),
] + router.urls
