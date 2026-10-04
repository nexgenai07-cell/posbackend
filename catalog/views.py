from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from branches.models import Branch
from common.mixins import BranchScopedQuerysetMixin
from inventory.services import check_products_stock

from .models import Category, Deal, Product
from .serializers import CategorySerializer, DealSerializer, ProductSerializer
from .services import current_price, is_available_today

class CategoryViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Read is any authenticated staff; write is owner/manager only. Queryset
    scoped to the requester's branch."""

    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def destroy(self, request, *args, **kwargs):
        category = self.get_object()
        if category.products.exists():
            return Response({"error": "error.categoryInUse"}, status=status.HTTP_409_CONFLICT)
        category.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Read is any authenticated staff (POS/KDS need the menu too); write is
    owner/manager only. Queryset scoped to the requester's branch."""

    serializer_class = ProductSerializer
    queryset = Product.objects.select_related("category").prefetch_related("deal__windows")

    def get_queryset(self):
        qs = super().get_queryset()
        category_id = self.request.query_params.get("category")
        available = self.request.query_params.get("available")
        if category_id:
            qs = qs.filter(category_id=category_id)
        if available is not None:
            qs = qs.filter(is_available=available.lower() in ("1", "true", "yes"))
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        products = list(page if page is not None else queryset)
        stock_status = check_products_stock(products)
        for product in products:
            product._is_orderable = is_available_today(product) and stock_status.get(product.pk, (False, None))[0]
        serializer = self.get_serializer(products, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def destroy(self, request, *args, **kwargs):
        """
        Drops the product's recipe first — matching the original mock's own
        comment ("an orphaned recipe costs nothing but confuses everyone").
        Necessary because soft-delete never invokes Django's real delete
        collector, so RecipeItem's on_delete=CASCADE never fires here on
        its own; without this, deleting a product would leave its recipe
        rows behind, still pointing at a now-soft-deleted product.
        """
        product = self.get_object()
        product.recipe_items.all().delete()
        product.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductDealView(APIView):
    """PUT creates-or-replaces the product's deal; DELETE removes it."""

    permission_classes = [IsOwnerOrManager]

    def put(self, request, pk):
        product = get_object_or_404(Product, pk=pk, branch=request.user.branch)
        serializer = DealSerializer(data=request.data, context={"product": product})
        serializer.is_valid(raise_exception=True)
        deal = serializer.save()
        return Response(DealSerializer(deal).data)

    def delete(self, request, pk):
        product = get_object_or_404(Product, pk=pk, branch=request.user.branch)
        # Hard delete: Deal.product is a OneToOneField, so a soft-deleted row
        # would still occupy the unique slot and block creating a new deal.
        deal = Deal.all_objects.filter(product=product).first()
        if deal:
            deal.hard_delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicMenuView(APIView):
    """
    Public (AllowAny) — today's available menu for a branch, with active deal
    pricing already resolved server-side (Phase 13: a public client can't be
    trusted to compute its own price). Mirrors restaurant-admin's
    getCustomerMenu() (src/lib/api/products.ts) plus lib/deals.ts's
    activeDealPrice()/lib/weekday.ts's isAvailableToday(), ported here.

    "Now" is resolved in the branch's own IANA timezone (Branch.timezone),
    not server/UTC time — a deal window like 11:00-14:00 means the
    restaurant's local clock, not wherever the server happens to run.
    `?branch=` is optional, same fallback-to-lowest-id convention as
    LoginChoicesView (single seeded branch today).
    """

    permission_classes = [AllowAny]
    throttle_scope = "menu"

    def get(self, request):
        branch_id = request.query_params.get("branch")
        if branch_id:
            branch = get_object_or_404(Branch, pk=branch_id)
        else:
            branch = Branch.objects.order_by("id").first()
            if branch is None:
                return Response({"error": "error.noBranch"}, status=status.HTTP_404_NOT_FOUND)

        categories = Category.objects.filter(branch=branch).order_by("sort_order")
        products = (
            Product.objects.filter(branch=branch, is_available=True)
            .select_related("category", "branch")
            .prefetch_related("deal__windows", "recipe_items__inventory_item")
        )
        products = list(products)
        stock_status = check_products_stock(products)

        by_category = {}
        for product in products:
            if not is_available_today(product):
                continue
            if not stock_status.get(product.pk, (False, None))[0]:
                continue

            # current_price() is the single source of truth for deal pricing —
            # also used by AddOrderItemSerializer, so what an order actually
            # charges can never drift from what this menu just displayed.
            price = current_price(product)
            on_deal = price != product.price

            by_category.setdefault(product.category_id, []).append({
                "id": product.id,
                "name_en": product.name_en,
                "name_ar": product.name_ar,
                "description_en": product.description_en,
                "description_ar": product.description_ar,
                "price": str(price),
                "original_price": str(product.price) if on_deal else None,
                "on_deal": on_deal,
                "image": product.image,
                "badge": "Deal" if on_deal else product.badge,
            })

        results = [
            {
                "id": category.id,
                "name_en": category.name_en,
                "name_ar": category.name_ar,
                "sort_order": category.sort_order,
                "products": by_category[category.id],
            }
            for category in categories
            if category.id in by_category
        ]

        return Response({
            "branch": {"id": branch.id, "name_en": branch.name_en, "name_ar": branch.name_ar, "currency": branch.currency},
            "categories": results,
        })
