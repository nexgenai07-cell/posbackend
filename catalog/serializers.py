from rest_framework import serializers

from common.validators import reject_branch_mismatch, require_bilingual_pair
from inventory.services import check_product_stock

from .models import Category, Deal, DealWindow, Product, Weekday


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

    class Meta:
        model = Product
        fields = [
            "id", "branch", "category", "name_en", "name_ar",
            "description_en", "description_ar", "price", "cost_price",
            "image", "is_available", "is_orderable", "badge", "days", "deal",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_days(self, value):
        valid_days = {choice for choice, _ in Weekday.choices}
        if not isinstance(value, list) or not all(day in valid_days for day in value):
            raise serializers.ValidationError("error.invalidWeekday")
        return value

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
