from rest_framework.routers import DefaultRouter

from .views import PurchaseViewSet, SupplierViewSet

router = DefaultRouter()
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("purchases", PurchaseViewSet, basename="purchase")

urlpatterns = router.urls
