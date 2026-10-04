from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.forms.models import BaseInlineFormSet

from common.admin import LIFECYCLE_FIELDSET, SoftDeleteAdmin

# Cross-app import, in the admin layer only: RecipeItem belongs to inventory
# (inventory/models.py already imports catalog.models), and a product's recipe
# is exactly what you want to see while editing that product. Same
# one-directional reasoning as the orders import in tables/views.py.
from inventory.models import RecipeItem

from .models import Category, Deal, DealWindow, Product


class RecipeItemInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        ingredient_ids = []
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            ingredient = form.cleaned_data.get("inventory_item")
            if ingredient is not None:
                ingredient_ids.append(ingredient.pk)
        if len(ingredient_ids) != len(set(ingredient_ids)):
            raise ValidationError("An ingredient can appear only once in a recipe.")


class RecipeItemInline(admin.TabularInline):
    """How much of each ingredient one unit of this product uses. Empty is
    normal: firing a product with no recipe simply deducts no stock."""

    model = RecipeItem
    extra = 0
    fields = ("inventory_item", "quantity", "unit")
    autocomplete_fields = ("inventory_item",)
    formset = RecipeItemInlineFormSet


@admin.register(Category)
class CategoryAdmin(SoftDeleteAdmin):
    list_display = ("id", "name_en", "name_ar", "branch", "sort_order", "product_count", "is_deleted")
    list_editable = ("sort_order",)
    list_filter = ("branch", "is_deleted")
    search_fields = ("name_en", "name_ar")
    list_select_related = ("branch",)
    autocomplete_fields = ("branch",)

    def get_queryset(self, request):
        """Counted in the same query, not once per row."""
        return (
            super()
            .get_queryset(request)
            # is_deleted filtered explicitly because reverse joins use an
            # unfiltered manager (see common/admin.py SoftDeleteAdmin).
            .annotate(_products=Count("products", filter=Q(products__is_deleted=False)))
        )

    @admin.display(description="products", ordering="_products")
    def product_count(self, obj):
        return obj._products


@admin.register(Product)
class ProductAdmin(SoftDeleteAdmin):
    list_display = (
        "id",
        "name_en",
        "category",
        "price",
        "cost_price",
        "margin",
        "is_available",
        "badge",
        "is_deleted",
    )
    list_editable = ("price", "is_available")
    list_filter = ("branch", "category", "is_available", "badge", "is_deleted")
    search_fields = ("name_en", "name_ar", "description_en", "description_ar")
    list_select_related = ("branch", "category")
    autocomplete_fields = ("branch", "category")
    inlines = [RecipeItemInline]
    fieldsets = (
        (
            "Naming",
            {"fields": ("branch", "category", "name_en", "name_ar", "description_en", "description_ar", "image")},
        ),
        (
            "Pricing",
            {
                "fields": ("price", "cost_price"),
                "description": "Selling price and what one unit costs to make; the changelist shows the resulting margin.",
            },
        ),
        (
            "Availability",
            {
                "fields": ("is_available", "badge", "days"),
                "description": (
                    'days is a JSON list of the weekdays this item is sold on, e.g. ["fri", "sat"] '
                    "(sun, mon, tue, wed, thu, fri, sat). An empty list means every day."
                ),
            },
        ),
        LIFECYCLE_FIELDSET,
    )

    @admin.display(description="margin")
    def margin(self, obj):
        """Gross margin %, which is the only reason both price fields sit on
        this screen together."""
        if not obj.price:
            return "-"
        return f"{(1 - obj.cost_price / obj.price) * 100:.0f}%"


class DealWindowInline(admin.TabularInline):
    """The hours a deal applies in. A deal with no window is always on."""

    model = DealWindow
    extra = 0
    fields = ("day", "start_time", "end_time")


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    """
    Deals are the one documented exception to soft delete: `Deal.product` is a
    OneToOneField, so a soft-deleted deal would keep occupying that product
    forever and the product's next deal would fail the unique constraint.
    catalog/views.py ProductDealView.delete() hard-deletes for exactly that
    reason, and this matches it — which is also why this admin does not extend
    SoftDeleteAdmin and has no is_deleted column to show.
    """

    list_display = ("id", "product", "price", "window_count")
    list_select_related = ("product",)
    search_fields = ("product__name_en", "product__name_ar")
    autocomplete_fields = ("product",)
    readonly_fields = ("created_at", "updated_at")
    inlines = [DealWindowInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _windows=Count("windows", filter=Q(windows__is_deleted=False))
        )

    @admin.display(description="windows", ordering="_windows")
    def window_count(self, obj):
        return obj._windows

    # Real DELETEs, not soft ones — see the class docstring.
    def delete_model(self, request, obj):
        obj.hard_delete()

    def delete_queryset(self, request, queryset):
        queryset.hard_delete()


@admin.register(DealWindow)
class DealWindowAdmin(SoftDeleteAdmin):
    list_display = ("id", "deal", "day", "start_time", "end_time", "is_deleted")
    list_filter = ("day", "is_deleted")
    search_fields = ("deal__product__name_en", "deal__product__name_ar")
    list_select_related = ("deal__product",)
    autocomplete_fields = ("deal",)
