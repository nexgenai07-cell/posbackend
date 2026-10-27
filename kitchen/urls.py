from rest_framework.routers import DefaultRouter

from .views import KitchenStationViewSet

router = DefaultRouter()
router.register("kitchen-stations", KitchenStationViewSet, basename="kitchen-station")

urlpatterns = router.urls
