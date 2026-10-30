from rest_framework.routers import DefaultRouter

from .views import ExpenseViewSet, PurchaseViewSet, SupplierViewSet

router = DefaultRouter()
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("purchases", PurchaseViewSet, basename="purchase")
router.register("expenses", ExpenseViewSet, basename="expense")

urlpatterns = router.urls
