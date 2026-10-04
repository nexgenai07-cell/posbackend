from decimal import Decimal

from django.contrib import admin
from django.db.models import Sum, Q

from common.admin import SoftDeleteAdmin

from .models import Order, OrderItem, Payment


class OrderItemInline(admin.TabularInline):
    """
    `*_snapshot` is shown next to the live product on purpose: order history
    prices from the snapshot, so a later menu price change never rewrites what
    a guest was charged — and seeing the two together is how a mismatch is
    spotted.
    """

    model = OrderItem
    extra = 0
    fields = ("product", "name_en_snapshot", "price_snapshot", "quantity", "status", "notes")
    autocomplete_fields = ("product",)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("amount", "method", "tip")


@admin.register(Order)
class OrderAdmin(SoftDeleteAdmin):
    list_display = (
        "id",
        "branch",
        "table",
        "status",
        "source",
        "staff",
        "item_count",
        "paid",
        "opened_at",
        "closed_at",
        "is_deleted",
    )
    list_filter = ("branch", "status", "source", "is_deleted")
    # Text fields only, no integer PKs: "icontains" against an integer column
    # is a hard error on Postgres, and no one searches orders by number here.
    search_fields = ("table__label_en", "table__label_ar", "customer__phone", "customer__name", "staff__name")
    list_select_related = ("branch", "table", "customer", "staff")
    autocomplete_fields = ("branch", "table", "customer", "staff")
    date_hierarchy = "opened_at"
    # Newest first is what a live order list wants; "id" breaks same-second
    # ties so pagination can't show the same order twice.
    ordering = ("-opened_at", "-id")
    inlines = [OrderItemInline, PaymentInline]

    def get_queryset(self, request):
        """Annotated/prefetched because `paid` and `item_count` would
        otherwise be two extra queries per row."""
        return (
            super()
            .get_queryset(request)
            # Conditional, because reverse joins use an unfiltered manager —
            # see common/admin.py SoftDeleteAdmin.
            .annotate(_paid=Sum("payments__amount", filter=Q(payments__is_deleted=False)))
            .prefetch_related("items")
        )

    @admin.display(description="items")
    def item_count(self, obj):
        return obj.items.count()

    @admin.display(description="paid", ordering="_paid")
    def paid(self, obj):
        """Sum of the payments, not of the item prices — an order can be
        split-tendered, and this is what the till actually took."""
        return obj._paid or Decimal("0")


@admin.register(OrderItem)
class OrderItemAdmin(SoftDeleteAdmin):
    """
    The back-office view of the rows the Kitchen Display shows. Status is
    editable straight from the list because bumping an item along is the whole
    point of that screen, and each save is a real .save(), so
    realtime/signals.py broadcasts it like any API call would.
    """

    list_display = ("id", "order", "name_en_snapshot", "quantity", "status", "is_deleted")
    list_editable = ("status",)
    list_filter = ("status", "is_deleted")
    search_fields = ("name_en_snapshot", "name_ar_snapshot", "product__name_en")
    list_select_related = ("order", "product")
    autocomplete_fields = ("product",)


@admin.register(Payment)
class PaymentAdmin(SoftDeleteAdmin):
    list_display = ("id", "order", "amount", "method", "tip", "paid_at", "is_deleted")
    list_filter = ("method", "is_deleted")
    search_fields = ("order__table__label_en", "order__customer__phone")
    list_select_related = ("order",)
    date_hierarchy = "paid_at"
    ordering = ("-paid_at", "-id")
