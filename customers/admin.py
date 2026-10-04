from django.contrib import admin

from common.admin import SoftDeleteAdmin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(SoftDeleteAdmin):
    """
    Rows are created by the public QR-ordering flow (find-or-create by phone),
    which isn't built yet — an empty changelist here is expected, not a bug
    (see customers/models.py).
    """

    list_display = ("id", "name", "phone", "branch", "last_order_at", "is_deleted")
    list_filter = ("branch", "is_deleted")
    # Phone is what the QR flow dedupes on, so it's the field to search by.
    search_fields = ("name", "phone")
    list_select_related = ("branch",)
    autocomplete_fields = ("branch",)
    date_hierarchy = "last_order_at"
