from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import InventoryItemViewSet, ProductRecipeView, RecipeListView, StockMovementListView

router = DefaultRouter()
router.register("inventory-items", InventoryItemViewSet, basename="inventory-item")

urlpatterns = [
    path("stock-movements/", StockMovementListView.as_view(), name="stock-movements"),
    path("recipes/", RecipeListView.as_view(), name="recipe-list"),
    path("products/<int:pk>/recipe/", ProductRecipeView.as_view(), name="product-recipe"),
] + router.urls
