from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CategoryViewSet, ProductDealView, ProductViewSet, PublicMenuView

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("products", ProductViewSet, basename="product")

urlpatterns = [
    path("menu/", PublicMenuView.as_view(), name="public-menu"),
    path("products/<int:pk>/deal/", ProductDealView.as_view(), name="product-deal"),
] + router.urls
