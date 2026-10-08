from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from branches.models import Branch
from common.mixins import BranchScopedQuerysetMixin
from inventory.services import check_product_stock, check_products_stock

from .models import Category, Deal, Product, ProductVariant
from .serializers import CategorySerializer, DealSerializer, ProductSerializer, ProductVariantSerializer
from .services import current_price, is_available_today

class CategoryViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Read is any authenticated staff; write is owner/manager only. Queryset
    scoped to the requester's branch."""

    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    def get_queryset(self):
        from django.db.models import Count
        from common.filters import text_filter
        qs = super().get_queryset()
        params = self.request.query_params
        qs = text_filter(qs, params, "search", ["name_en", "name_ar"])
        has_products = (params.get("has_products") or "").strip().lower()
        if has_products in ("true", "1", "yes"):
            qs = qs.annotate(product_count=Count("products")).filter(product_count__gt=0)
        elif has_products in ("false", "0", "no"):
            qs = qs.annotate(product_count=Count("products")).filter(product_count=0)
        return qs

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
        from django.db.models import Exists, OuterRef
        from common.filters import text_filter, numeric_range_filter
        qs = super().get_queryset()
        params = self.request.query_params
        category_id = params.get("category")
        available = params.get("available")
        if category_id:
            qs = qs.filter(category_id=category_id)
        if available is not None:
            qs = qs.filter(is_available=available.lower() in ("1", "true", "yes"))
        qs = text_filter(qs, params, "search", ["name_en", "name_ar", "description_en", "description_ar"])
        badge = (params.get("badge") or "").strip()
        if badge == "none":
            qs = qs.filter(badge="")
        elif badge and badge != "all":
            qs = qs.filter(badge=badge)
        qs = numeric_range_filter(qs, params, "price", "price_min", "price_max")
        has_recipe = (params.get("has_recipe") or "").strip().lower()
        if has_recipe in ("true", "1", "yes"):
            from inventory.models import RecipeItem
            qs = qs.filter(Exists(RecipeItem.objects.filter(product=OuterRef("pk"))))
        elif has_recipe in ("false", "0", "no"):
            from inventory.models import RecipeItem
            qs = qs.exclude(Exists(RecipeItem.objects.filter(product=OuterRef("pk"))))
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
            Product.objects.filter(branch=branch)
            .select_related("category", "branch")
            .prefetch_related("deal__windows", "recipe_items__inventory_item", "variants")
        )
        products = list(products)
        stock_status = check_products_stock(products)

        by_category = {}
        for product in products:
            is_orderable = (
                product.is_available
                and is_available_today(product)
                and stock_status.get(product.pk, (False, None))[0]
            )

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
                "is_available": product.is_available,
                "is_orderable": is_orderable,
                # Active variants only — an inactive size must not be
                # orderable. Each carries its own deal-resolved price via the
                # same current_price(), so the menu and the order snapshot
                # cannot disagree about what a 1 KG costs today.
                #
                # Deliberately NO cost, multiplier or margin here: this is an
                # AllowAny endpoint and those are staff figures.
                "variants": [
                    {
                        "id": variant.id,
                        "name_en": variant.name_en,
                        "name_ar": variant.name_ar,
                        "price": str(current_price(product, variant)),
                        "original_price": str(variant.price) if current_price(product, variant) != variant.price else None,
                        "on_deal": current_price(product, variant) != variant.price,
                        "is_orderable": is_orderable and check_product_stock(product, 1, variant=variant)[0],
                    }
                    for variant in product.variants.all()
                    if variant.is_active
                ],
            })

        results = [
            {
                "id": category.id,
                "name_en": category.name_en,
                "name_ar": category.name_ar,
                "sort_order": category.sort_order,
                "products": by_category.get(category.id, []),
            }
            for category in categories
        ]

        return Response({
            "branch": {"id": branch.id, "name_en": branch.name_en, "name_ar": branch.name_ar, "currency": branch.currency},
            "categories": results,
        })


class ProductVariantViewSet(viewsets.ModelViewSet):
    """
    Configurable portions for one product — /api/products/<id>/variants/.

    Read is any authenticated staff (POS and KDS both need to show which size
    was ordered); write is owner/manager only, matching every other
    administrative configuration surface (requirement §22).

    Scoped to the product in the URL AND to the requester's branch, so a
    manager of one branch cannot edit another branch's pricing by guessing an
    id — the same scoping rule _get_order_or_404 applies in orders/views.py.
    """

    serializer_class = ProductVariantSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def get_queryset(self):
        return ProductVariant.objects.filter(
            product_id=self.kwargs["product_pk"],
            product__branch=self.request.user.branch,
        ).select_related("product")

    def perform_create(self, serializer):
        product = get_object_or_404(
            Product, pk=self.kwargs["product_pk"], branch=self.request.user.branch,
        )
        serializer.save(product=product)
