from decimal import Decimal

from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from common.admin import SoftDeleteAdmin
from inventory.models import InventoryItem, StockMovementReason
from inventory.services import adjust_stock

from .models import Purchase, PurchaseItem, PurchaseStatus, Supplier


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0
    fields = ("inventory_item", "quantity", "unit_cost", "line_total")
    autocomplete_fields = ("inventory_item",)

    def get_readonly_fields(self, request, obj=None):
        # A received PO is immutable (purchasing/views.py refuses PATCH and
        # DELETE on one) because its lines already moved stock and rewrote each
        # item's cost_per_unit — editing them afterwards would silently desync
        # both.
        if obj and obj.status == PurchaseStatus.RECEIVED:
            return ("inventory_item", "quantity", "unit_cost", "line_total")
        return ("line_total",)

    @admin.display(description="line total")
    def line_total(self, obj):
        if not obj.pk:
            return "-"
        return obj.quantity * obj.unit_cost


@admin.register(Purchase)
class PurchaseAdmin(SoftDeleteAdmin):
    """
    Receiving is the only thing that moves stock: it calls adjust_stock() per
    line and rewrites each item's cost_per_unit (purchasing/views.py). So
    `status` is read-only here and that side effect lives in the "Mark as
    received" action below, which mirrors the API's locked, transactional
    receive — flipping the field by hand would leave stock untouched.
    """

    list_display = ("id", "supplier", "branch", "status", "ordered_at", "received_at", "total", "is_deleted")
    list_filter = ("branch", "status", "supplier", "is_deleted")
    search_fields = ("supplier__name_en", "supplier__name_ar")
    list_select_related = ("branch", "supplier")
    autocomplete_fields = ("branch", "supplier")
    date_hierarchy = "ordered_at"
    ordering = ("-ordered_at", "-id")
    readonly_fields = SoftDeleteAdmin.readonly_fields + ("status", "received_at")
    actions = ("mark_received",) + SoftDeleteAdmin.actions
    inlines = [PurchaseItemInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("items")

    @admin.display(description="total")
    def total(self, obj):
        """Quantity x unit cost per line, summed — what the PO is worth.
        Computed from the prefetched items, so no query per row."""
        return sum((item.quantity * item.unit_cost for item in obj.items.all()), Decimal("0"))

    @admin.action(description="Mark as received (moves stock, like the API)")
    def mark_received(self, request, queryset):
        """
        Per purchase: lock the row, add a StockMovement per line through
        adjust_stock() (which also updates current_stock), set each item's
        cost_per_unit to what was actually paid, then flip the status — the
        same sequence as the API's receive endpoint.
        """
        received = 0
        skipped = []
        for purchase in queryset.filter(is_deleted=False).exclude(status=PurchaseStatus.RECEIVED):
            with transaction.atomic():
                # all_objects, because the action's queryset came from
                # SoftDeleteAdmin and may point at a soft-deleted row.
                purchase = Purchase.all_objects.select_for_update().get(pk=purchase.pk)
                # Re-checked under the lock: a POS terminal can receive the same
                # PO at the same moment we do, and both would otherwise move
                # stock twice.
                if purchase.status == PurchaseStatus.RECEIVED:
                    skipped.append(purchase.pk)
                    continue
                # Re-fetching the lines instead of trusting a prefetch — the
                # same staleness lesson as SendToKitchenView in Phase 8.
                for line in purchase.items.select_related("inventory_item"):
                    adjust_stock(
                        line.inventory_item_id,
                        line.quantity,
                        StockMovementReason.PURCHASE,
                        purchase=purchase,
                        purchase_item=line,
                        unit_cost_snapshot=line.unit_cost,
                    )
                    InventoryItem.objects.filter(pk=line.inventory_item_id).update(cost_per_unit=line.unit_cost)
                purchase.status = PurchaseStatus.RECEIVED
                purchase.received_at = timezone.now()
                purchase.save(update_fields=["status", "received_at", "updated_at"])
                received += 1
        if received:
            self.message_user(request, f"Received {received} purchase(s); stock adjusted.")
        if skipped:
            self.message_user(
                request,
                f"Skipped {len(skipped)} purchase(s) that had just been received elsewhere.",
                level=messages.WARNING,
            )


@admin.register(PurchaseItem)
class PurchaseItemAdmin(SoftDeleteAdmin):
    list_display = ("id", "purchase", "inventory_item", "quantity", "unit_cost", "is_deleted")
    list_filter = ("is_deleted",)
    search_fields = ("inventory_item__name", "purchase__supplier__name_en")
    list_select_related = ("purchase", "purchase__supplier", "inventory_item")
    autocomplete_fields = ("purchase", "inventory_item")


@admin.register(Supplier)
class SupplierAdmin(SoftDeleteAdmin):
    """Not branch-scoped — the same supplier serves every branch
    (purchasing/views.py)."""

    list_display = ("id", "name_en", "name_ar", "contact_info", "purchase_count", "is_deleted")
    list_filter = ("is_deleted",)
    search_fields = ("name_en", "name_ar", "contact_info")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            # Conditional, because reverse joins use an unfiltered manager —
            # see common/admin.py SoftDeleteAdmin.
            .annotate(_purchases=Count("purchases", filter=Q(purchases__is_deleted=False)))
        )

    @admin.display(description="POs", ordering="_purchases")
    def purchase_count(self, obj):
        """Anything above zero is why the API refuses to delete this supplier
        (error.supplierInUse), so this column answers "why?"."""
        return obj._purchases
