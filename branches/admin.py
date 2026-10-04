from django.contrib import admin

from common.admin import LIFECYCLE_FIELDSET, SoftDeleteAdmin

from .models import Branch


@admin.register(Branch)
class BranchAdmin(SoftDeleteAdmin):
    """
    Single-branch today, but every other model hangs off this row, and
    `currency` / `timezone` are what the frontend formats every price and
    timestamp with — so a typo here shows up on nearly every screen.
    """

    list_display = ("id", "name_en", "name_ar", "address", "currency", "timezone", "is_deleted")
    list_filter = ("currency", "is_deleted")
    search_fields = ("name_en", "name_ar", "address")
    fieldsets = (
        ("Identity", {"fields": ("name_en", "name_ar", "address")}),
        (
            "Formatting",
            {
                "fields": ("currency", "timezone"),
                "description": (
                    "Used by the frontend for every price (currency, e.g. SAR) and every "
                    "timestamp (timezone, an IANA name such as Asia/Riyadh — not an offset)."
                ),
            },
        ),
        LIFECYCLE_FIELDSET,
    )
