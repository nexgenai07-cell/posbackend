"""
Shared admin building blocks.

Each app registers its own models (see the other admin.py files); this module
holds only what they all share, so the conventions below can't drift apart.
"""

from django.contrib import admin
from django.utils import timezone

# Every BaseModel has these three. Collapsed by default: they answer "when was
# this last touched, and is it deleted?" without taking space from the fields
# that model actually exists for.
LIFECYCLE_FIELDSET = (
    "Lifecycle",
    {"fields": ("created_at", "updated_at", "is_deleted"), "classes": ("collapse",)},
)


class SoftDeleteAdmin(admin.ModelAdmin):
    """
    Base for every admin backed by common.models.BaseModel.

    BaseModel soft-deletes: `.delete()` flips `is_deleted`, and the default
    manager then hides the row from every queryset. The admin deliberately
    contradicts that default in two ways, because whoever is looking at the
    data needs the truth, not the app's filtered view of it:

    * the changelist queries `all_objects`, so a soft-deleted row stays visible
      with `is_deleted` ticked instead of silently vanishing the moment someone
      clicks "Delete selected";
    * `is_deleted` is read-only in the detail form — rows are removed and
      restored through the two actions below, never by hand-editing the flag.

    Both are intentional. With soft delete, "where did my row go?" is the most
    confusing thing an admin can do, and the answer has to be on screen.

    Two caveats worth knowing before using these actions on live data:

    * Both are a single bulk UPDATE, which does *not* fire post_save /
      post_delete, so no WebSocket event is published — the same limitation
      realtime/signals.py documents for bulk updates. Deleting one row at a
      time from its own page is a real .save() and does broadcast. Anything the
      POS is watching (tables, orders) is therefore best changed through the
      API, or through the Table actions in tables/admin.py.
    * Counts and filters that join *through* a relation don't inherit this
      soft-delete filter: BaseModel never sets Meta.base_manager_name, so
      reverse joins use an unfiltered manager. Aggregate columns that mean "how
      many live rows" filter `is_deleted` explicitly to say so.

    Hard delete is deliberately not offered as an action: BaseModel.hard_delete()
    is an escape hatch that deserves a documented call site, not a checkbox.
    """

    readonly_fields = ("created_at", "updated_at", "is_deleted")
    actions = ("restore_selected", "soft_delete_selected")

    # 50 rows keeps a phone-book-sized list on one page without being slow.
    list_per_page = 50
    save_on_top = True

    def get_queryset(self, request):
        """all_objects, not the default manager — see the class docstring."""
        return self.model.all_objects.all()

    @admin.action(description="Restore selected (clear is_deleted)")
    def restore_selected(self, request, queryset):
        restored = queryset.filter(is_deleted=True).update(is_deleted=False, updated_at=timezone.now())
        self.message_user(request, f"Restored {restored} row(s).")

    @admin.action(description="Soft-delete selected (rows are kept)")
    def soft_delete_selected(self, request, queryset):
        alive = queryset.filter(is_deleted=False)
        count = alive.count()
        # SoftDeleteQuerySet.delete() -> UPDATE is_deleted = True.
        alive.delete()
        self.message_user(request, f"Soft-deleted {count} row(s); they are still listed, marked as deleted.")
