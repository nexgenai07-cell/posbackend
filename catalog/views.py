from common.pagination import ListMetadataMixin
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from branches.models import Branch
from common.mixins import BranchScopedQuerysetMixin
from inventory.services import availability_from, stock_requirements, units_from, variant_multiplier

from .models import Category, Deal, Product, ProductVariant
from .serializers import CategorySerializer, DealSerializer, ProductSerializer, ProductVariantSerializer
from .services import current_price, is_available_today

class CategoryViewSet(ListMetadataMixin, BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Read is any authenticated staff; write is owner/manager only. Queryset
    scoped to the requester's branch."""

    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    def get_queryset(self):
        from django.db.models import Count, Q
        from common.filters import text_filter
        qs = super().get_queryset().annotate(product_count=Count("products", filter=Q(products__is_deleted=False), distinct=True))
        params = self.request.query_params
        qs = text_filter(qs, params, "search", ["name_en", "name_ar"])
        has_products = (params.get("has_products") or "").strip().lower()
        if has_products in ("true", "1", "yes"):
            qs = qs.filter(product_count__gt=0)
        elif has_products in ("false", "0", "no"):
            qs = qs.filter(product_count=0)
        from common.filters import ordering_filter
        return ordering_filter(qs, params, {"name": "name_ar" if params.get("language") == "ar" else "name_en", "products": "product_count", "order": "sort_order"}, ["sort_order"])

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


class ProductViewSet(ListMetadataMixin, BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """Read is any authenticated staff (POS/KDS need the menu too); write is
    owner/manager only. Queryset scoped to the requester's branch."""

    serializer_class = ProductSerializer
    # Everything the serializer reads per product, loaded up front: branch
    # (availability / deal timezone), variants, and recipe lines with their
    # ingredient (live costing). Without these the list ran ~10 queries per
    # product, which on a remote database made the POS menu take seconds.
    queryset = Product.objects.select_related("category", "branch").prefetch_related(
        "deal__windows", "variants", "recipe_items__inventory_item",
    )

    def get_queryset(self):
        from django.db.models import Exists, OuterRef, Count, Q
        from common.filters import text_filter, numeric_range_filter
        qs = super().get_queryset().annotate(recipe_count=Count("recipe_items", filter=Q(recipe_items__is_deleted=False), distinct=True))
        params = self.request.query_params
        category_id = params.get("category")
        available = params.get("available")
        if category_id:
            qs = qs.filter(category_id=category_id)
        if available is not None:
            qs = qs.filter(is_available=available.lower() in ("1", "true", "yes"))
        qs = text_filter(qs, params, "search", ["name_en", "name_ar", "description_en", "description_ar", "category__name_en", "category__name_ar"])
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
        if params.get("has_deal") == "true":
            qs = qs.filter(deal__isnull=False)
        from common.filters import ordering_filter
        language = "ar" if params.get("language") == "ar" else "en"
        return ordering_filter(qs, params, {"name": f"name_{language}", "category": f"category__name_{language}", "price": "price", "available": "is_available", "ingredients": "recipe_count", "cost": "cost_price"}, ["name_en"])

    def list_metadata(self, qs):
        return {"with_recipe": sum(product.recipe_count > 0 for product in qs) if isinstance(qs, list) else qs.filter(recipe_count__gt=0).count()}


    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        ordering = request.query_params.get("ordering", "")
        if ordering.lstrip("-") == "margin":
            from .costing import recipe_cost
            def margin(product):
                cost = recipe_cost(product) if product.recipe_count else product.cost_price
                return (product.price - cost) / product.price if product.price else 0
            # Recipe/unit-aware margin has no stored sortable column. Compute
            # on the server before slicing, reusing prefetched recipe lines.
            queryset = sorted(queryset, key=lambda product: (margin(product), product.pk), reverse=ordering.startswith("-"))
        page = self.paginate_queryset(queryset)
        products = list(page if page is not None else queryset)
        items, requirements = stock_requirements(products)
        for product in products:
            requirement = requirements.get(product.pk, "missing_recipe")
            product._is_orderable = is_available_today(product) and availability_from(items, requirement)[0]
            product._available_quantity = units_from(items, requirement) if product._is_orderable else 0
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


# The public menu reports how many of an item can still be added, capped so it
# never discloses real stock levels — enough for the page to stop "+" in time.
PUBLIC_QUANTITY_CAP = 20


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
        # Recipes and stock read once for the whole menu; every product and
        # every size below is checked against this same snapshot.
        items, requirements = stock_requirements(products)

        by_category = {}
        for product in products:
            requirement = requirements.get(product.pk, "missing_recipe")
            is_orderable = (
                product.is_available
                and is_available_today(product)
                and availability_from(items, requirement)[0]
            )
            # .all() reuses the prefetched recipe; .exists() queried per product.
            has_recipe = bool(product.recipe_items.all())

            # This endpoint is the customer-facing menu: hidden, unscheduled,
            # out-of-stock, or unconfigured products should not be sent to
            # customer clients at all. Staff endpoints still return these
            # products so the admin can manage them.
            if not is_orderable or not has_recipe:
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
                "is_available": product.is_available,
                "is_orderable": is_orderable,
                # How many the customer can still add, so the menu and cart
                # disable "+" at the limit. Capped: this endpoint is public,
                # and the exact stock level is not a customer's business.
                "available_quantity": min(units_from(items, requirement), PUBLIC_QUANTITY_CAP),
                "has_recipe": has_recipe,
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
                        "is_orderable": is_orderable and availability_from(items, requirement, 1, variant_multiplier(variant))[0],
                        "available_quantity": min(
                            units_from(items, requirement, variant.recipe_multiplier),
                            PUBLIC_QUANTITY_CAP,
                        ) if is_orderable else 0,
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
