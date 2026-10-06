from decimal import Decimal

from rest_framework import serializers

from common.validators import reject_branch_mismatch, require_bilingual_pair
from inventory.services import check_product_stock

from .models import Category, Deal, DealWindow, Product, ProductVariant, Weekday


class CategorySerializer(serializers.ModelSerializer):
    # Neither language is unconditionally required — see require_bilingual_pair
    # below, which only requires whichever one the admin UI has active.
    name_en = serializers.CharField(required=False, allow_blank=True)
    name_ar = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Category
        fields = ["id", "branch", "name_en", "name_ar", "sort_order", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        reject_branch_mismatch(self.context.get("request"), branch)
        require_bilingual_pair(attrs, self.instance, "name")

        name_en = attrs.get("name_en", getattr(self.instance, "name_en", None))
        if name_en:
            qs = Category.objects.filter(branch=branch, name_en__iexact=name_en.strip())
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"name_en": "error.duplicateName"})
        return attrs


class DealWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = DealWindow
        fields = ["day", "start_time", "end_time"]


class DealSerializer(serializers.ModelSerializer):
    windows = DealWindowSerializer(many=True)

    class Meta:
        model = Deal
        fields = ["price", "windows"]

    def validate(self, attrs):
        product = self.context["product"]
        if attrs["price"] >= product.price:
            raise serializers.ValidationError({"price": "error.dealPriceTooHigh"})

        windows = attrs.get("windows") or []
        if not windows:
            raise serializers.ValidationError({"windows": "error.dealWindowsRequired"})
        for window in windows:
            if window["start_time"] >= window["end_time"]:
                raise serializers.ValidationError({"windows": "error.dealWindowInvalid"})
        return attrs

    def create(self, validated_data):
        windows_data = validated_data.pop("windows")
        product = self.context["product"]
        deal, _ = Deal.objects.update_or_create(product=product, defaults=validated_data)
        deal.windows.all().delete()
        DealWindow.objects.bulk_create([DealWindow(deal=deal, **window) for window in windows_data])
        return deal



class ProductVariantSerializer(serializers.ModelSerializer):
    """
    One configurable portion of a product. The restaurant supplies the name and
    price; nothing here assumes what a variant represents.

    `costing` is DERIVED live from current ingredient prices — see
    catalog/costing.py. It is staff-only data and is never exposed on the
    public menu.
    """

    name_en = serializers.CharField(required=False, allow_blank=True)
    name_ar = serializers.CharField(required=False, allow_blank=True)
    price = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0,
        error_messages={"min_value": "error.priceInvalid", "required": "error.priceInvalid", "invalid": "error.priceInvalid"},
    )
    recipe_multiplier = serializers.DecimalField(
        max_digits=6, decimal_places=3, min_value=Decimal("0.001"), max_value=Decimal("999"), required=False,
        error_messages={
            "min_value": "error.recipeMultiplierInvalid",
            "max_value": "error.recipeMultiplierInvalid",
            "invalid": "error.recipeMultiplierInvalid",
        },
    )
    costing = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id", "product", "name_en", "name_ar", "price", "recipe_multiplier",
            "is_active", "sort_order", "costing", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "product", "costing", "created_at", "updated_at"]

    def get_costing(self, variant):
        from .costing import costing as compute_costing
        return compute_costing(variant.product, variant)

    def _target_product(self):
        """
        The product this variant belongs to.

        On update it comes from the instance; on create it comes from the URL
        (/api/products/<product_pk>/variants/), which is why `product` is
        read-only above — a nested resource must not let the body point the
        variant at a different product than the URL says.
        """
        if self.instance is not None:
            return self.instance.product
        product_pk = (self.context.get("view").kwargs.get("product_pk")
                      if self.context.get("view") else None)
        if not product_pk:
            return None
        return Product.objects.filter(pk=product_pk).select_related("branch").first()

    def validate(self, attrs):
        require_bilingual_pair(attrs, self.instance, "name")

        product = self._target_product()
        if product is not None:
            reject_branch_mismatch(self.context.get("request"), product.branch)

        # Two "1 KG" rows on the same product would make the menu ambiguous and
        # the order snapshot meaningless. Mirrors the name_en-only uniqueness
        # convention used for categories, products and tables.
        name_en = attrs.get("name_en", getattr(self.instance, "name_en", None))
        if name_en and product is not None:
            duplicates = ProductVariant.objects.filter(
                product=product, name_en__iexact=name_en.strip(),
            )
            if self.instance:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise serializers.ValidationError({"name_en": "error.variantNameExists"})
        return attrs


class ProductSerializer(serializers.ModelSerializer):
    # Neither language is unconditionally required — see require_bilingual_pair
    # in validate(), which only requires whichever one the admin UI has active.
    name_en = serializers.CharField(required=False, allow_blank=True)
    name_ar = serializers.CharField(required=False, allow_blank=True)
    price = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        error_messages={"min_value": "error.priceInvalid", "required": "error.priceInvalid", "invalid": "error.priceInvalid"},
    )
    cost_price = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        error_messages={"min_value": "error.costPriceInvalid", "required": "error.costPriceInvalid", "invalid": "error.costPriceInvalid"},
    )
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        error_messages={"required": "error.categoryRequired", "does_not_exist": "error.categoryRequired"},
    )
    deal = DealSerializer(read_only=True)
    is_orderable = serializers.SerializerMethodField()
    variants = ProductVariantSerializer(many=True, read_only=True)
    # Live margin figures, recomputed from current InventoryItem.cost_per_unit
    # on every read. Never stored, so a new purchase moves these immediately.
    costing = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "branch", "category", "name_en", "name_ar",
            "description_en", "description_ar", "price", "cost_price",
            "image", "is_available", "is_orderable", "badge", "days", "deal",
            "variants", "costing",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "variants", "costing", "created_at", "updated_at"]

    def validate_days(self, value):
        valid_days = {choice for choice, _ in Weekday.choices}
        if not isinstance(value, list) or not all(day in valid_days for day in value):
            raise serializers.ValidationError("error.invalidWeekday")
        return value

    def get_costing(self, product):
        from .costing import costing as compute_costing
        return compute_costing(product)

    def get_is_orderable(self, product):
        if hasattr(product, "_is_orderable"):
            return product._is_orderable
        from .services import is_available_today

        if not is_available_today(product):
            return False
        available, _reason = check_product_stock(product)
        return available

    def validate(self, attrs):
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        reject_branch_mismatch(self.context.get("request"), branch)
        require_bilingual_pair(attrs, self.instance, "name")

        name_en = attrs.get("name_en", getattr(self.instance, "name_en", None))
        if name_en:
            qs = Product.objects.filter(branch=branch, name_en__iexact=name_en.strip())
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"name_en": "error.duplicateName"})
        return attrs
