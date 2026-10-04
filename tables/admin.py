from django.contrib import admin, messages

from common.admin import SoftDeleteAdmin

# Imported in the admin layer, not in tables/models.py, to keep the model
# layer's dependency one-directional — same reasoning as tables/views.py.
from orders.models import OrderStatus

from .models import Table


@admin.register(Table)
class TableAdmin(SoftDeleteAdmin):
    """
    `session_token` and `qr_code` are generated once per table and remain stable.
    Status changes through Table.open()/close()/mark_needs_bill(), so derived
    fields stay read-only.
    the actions below call the same model methods the POS endpoints do
    (tables/views.py).
    """

    list_display = ("id", "label_en", "label_ar", "branch", "status", "has_session", "is_deleted")
    list_filter = ("branch", "status", "is_deleted")
    search_fields = ("label_en", "label_ar", "qr_code")
    list_select_related = ("branch",)
    autocomplete_fields = ("branch",)
    readonly_fields = SoftDeleteAdmin.readonly_fields + ("session_token", "qr_code")
    actions = ("open_sessions", "mark_needs_bill", "close_sessions")

    @admin.display(boolean=True, description="open session")
    def has_session(self, obj):
        return bool(obj.session_token)

    @admin.action(description="Open table (mark occupied)")
    def open_sessions(self, request, queryset):
        """Mints a fresh token per table, which invalidates any QR code already
        printed for it — that is what the POS's open action does, not a side
        effect invented here."""
        for table in queryset:
            table.open()
        self.message_user(request, f"Opened {queryset.count()} table(s).")

    @admin.action(description="Mark needs bill")
    def mark_needs_bill(self, request, queryset):
        for table in queryset:
            table.mark_needs_bill()
        self.message_user(request, f"Marked {queryset.count()} table(s) as needs-bill.")

    @admin.action(description="Close table (mark empty; preserve QR)")
    def close_sessions(self, request, queryset):
        """Mirrors the API's guard: a table with an order that isn't closed or
        cancelled can't be released, or the bill would lose track of its
        table."""
        blocked = []
        for table in queryset:
            has_open_order = table.orders.exclude(
                status__in=[OrderStatus.CLOSED, OrderStatus.CANCELLED]
            ).exists()
            if has_open_order:
                blocked.append(table.label_en)
                continue
            table.close()
        closed = queryset.count() - len(blocked)
        if closed:
            self.message_user(request, f"Closed {closed} table(s).")
        if blocked:
            self.message_user(
                request,
                f"Skipped {len(blocked)} table(s) with an open order: {', '.join(blocked)}",
                level=messages.WARNING,
            )
