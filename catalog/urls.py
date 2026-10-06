from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CategoryViewSet,
    ProductDealView,
    ProductVariantViewSet,
    ProductViewSet,
    PublicMenuView,
)

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("products", ProductViewSet, basename="product")

# Variants are nested under their product rather than registered flat: a
# variant has no meaning apart from the product it portions, and nesting makes
# the branch/product scoping in ProductVariantViewSet.get_queryset() the only
# way to reach one.
variant_list = ProductVariantViewSet.as_view({"get": "list", "post": "create"})
variant_detail = ProductVariantViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "put": "update", "delete": "destroy"}
)

urlpatterns = [
    path("menu/", PublicMenuView.as_view(), name="public-menu"),
    path("products/<int:pk>/deal/", ProductDealView.as_view(), name="product-deal"),
    path("products/<int:product_pk>/variants/", variant_list, name="product-variant-list"),
    path("products/<int:product_pk>/variants/<int:pk>/", variant_detail, name="product-variant-detail"),
] + router.urls
