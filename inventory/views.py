from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from catalog.models import Product
from common.mixins import BranchScopedQuerysetMixin

from .models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from .serializers import (
    AdjustStockSerializer,
    InventoryItemSerializer,
    RecipeItemSerializer,
    RecipeRowSerializer,
    StockMovementSerializer,
)
from .services import adjust_stock


class InventoryItemViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Inventory quantities and mutations are restricted to owner/manager."""

    queryset = InventoryItem.objects.select_related("supplier")
    serializer_class = InventoryItemSerializer

    def get_queryset(self):
        from common.filters import text_filter, choice_filter, fk_filter
        qs = super().get_queryset()
        params = self.request.query_params
        qs = text_filter(qs, params, "search", ["name"])
        qs = fk_filter(qs, params, "supplier", field="supplier_id")
        qs = choice_filter(qs, params, "unit", field="unit")
        stock_status = (params.get("stock_status") or "").strip()
        if stock_status == "out":
            qs = qs.filter(current_stock__lte=0)
        elif stock_status == "low":
            qs = qs.filter(current_stock__gt=0, current_stock__lte=F("par_level"))
        elif stock_status == "ok":
            qs = qs.filter(current_stock__gt=F("par_level"))
        return qs

    def get_permissions(self):
        return [IsOwnerOrManager()]

    def destroy(self, request, *args, **kwargs):
        item = self.get_object()
        if item.recipe_items.exists():
            return Response({"error": "error.inventoryItemInUse"}, status=status.HTTP_409_CONFLICT)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_create(self, serializer):
        initial_stock = serializer.validated_data.get("current_stock", 0)
        with transaction.atomic():
            item = serializer.save(current_stock=0)
            if initial_stock:
                updated = adjust_stock(item.pk, initial_stock, StockMovementReason.ADJUSTMENT)
                item.current_stock = updated.current_stock

    @action(detail=False, methods=["get"], url_path="low-stock")
    def low_stock(self, request):
        qs = self.filter_queryset(self.get_queryset()).filter(current_stock__lte=F("par_level"))
        page = self.paginate_queryset(qs)
        serializer = self.get_serializer(page if page is not None else qs, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="adjust")
    def adjust(self, request, pk=None):
        item = self.get_object()
        serializer = AdjustStockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                updated = adjust_stock(
                    item.pk,
                    serializer.validated_data["quantity_delta"],
                    serializer.validated_data["reason"],
                )
        except ValidationError:
            return Response({"error": "error.stockAdjustmentBelowZero"}, status=status.HTTP_409_CONFLICT)
        return Response(InventoryItemSerializer(updated).data)


class StockMovementListView(ListAPIView):
    """Read-only — StockMovement is append-only, nothing here can update or
    delete one."""

    serializer_class = StockMovementSerializer
    permission_classes = [IsOwnerOrManager]

    def get_queryset(self):
        qs = StockMovement.objects.filter(inventory_item__branch=self.request.user.branch).select_related("inventory_item", "order")
        item_id = self.request.query_params.get("item")
        if item_id:
            qs = qs.filter(inventory_item_id=item_id)
        return qs


def _sync_product_cost_price(product):
    """
    Keep Product.cost_price in step with the recipe it was derived from.

    Live margin figures are computed on the fly (catalog/costing.py), but
    reports/ aggregates on the stored `product__cost_price` column, so the two
    would silently diverge if saving a recipe left the column stale. Writing it
    here means the admin never types a cost that the ingredients already know.
    """
    from catalog.costing import recipe_cost

    Product.objects.filter(pk=product.pk).update(cost_price=recipe_cost(product))


class ProductRecipeView(APIView):
    """GET the recipe, PUT replaces the whole set of ingredients, DELETE
    clears it — same replace-all shape as catalog's ProductDealView."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        product = get_object_or_404(Product, pk=pk, branch=request.user.branch)
        items = RecipeItem.objects.filter(product=product).select_related("inventory_item")
        return Response(RecipeItemSerializer(items, many=True).data)

    def put(self, request, pk):
        product = get_object_or_404(Product, pk=pk, branch=request.user.branch)
        serializer = RecipeItemSerializer(data=request.data, many=True, context={"product": product})
        serializer.is_valid(raise_exception=True)

        # RecipeItemSerializer's inventory_item field isn't branch-scoped
        # (it's a plain PrimaryKeyRelatedField over all InventoryItems), so
        # cross-branch references need an explicit check here.
        inventory_item_ids = {item["inventory_item"].id for item in serializer.validated_data}
        if len(inventory_item_ids) != len(serializer.validated_data):
            return Response({"error": "error.duplicateRecipeIngredient"}, status=status.HTTP_400_BAD_REQUEST)
        valid_count = InventoryItem.objects.filter(pk__in=inventory_item_ids, branch=request.user.branch).count()
        if valid_count != len(inventory_item_ids):
            return Response({"error": "error.inventoryItemInvalid"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            RecipeItem.objects.filter(product=product).delete()
            RecipeItem.objects.bulk_create([RecipeItem(product=product, **item) for item in serializer.validated_data])
            _sync_product_cost_price(product)

        items = RecipeItem.objects.filter(product=product).select_related("inventory_item")
        return Response(RecipeItemSerializer(items, many=True).data)

    def delete(self, request, pk):
        product = get_object_or_404(Product, pk=pk, branch=request.user.branch)
        RecipeItem.objects.filter(product=product).delete()
        # No recipe left, so there is nothing to derive a cost from. Left as it
        # was rather than zeroed: a product may legitimately have a hand-entered
        # cost (a bought-in drink), and blanking it would misreport margins.
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecipeListView(ListAPIView):
    """
    Every recipe line in the requester's branch, in one call.

    The per-product endpoints above can't answer "which products use this
    ingredient?" without the client firing one request per product, which is
    exactly what the admin's Recipes, Stock and Menu screens need.
    """

    serializer_class = RecipeRowSerializer
    permission_classes = [IsOwnerOrManager]

    def get_queryset(self):
        return (
            RecipeItem.objects.filter(product__branch=self.request.user.branch)
            .select_related("product", "inventory_item")
            .order_by("product_id", "id")
        )
