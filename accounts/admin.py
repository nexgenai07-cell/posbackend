from django import forms
from django.contrib import admin

from common.admin import LIFECYCLE_FIELDSET, SoftDeleteAdmin

from .models import Shift, Staff


class StaffAdminForm(forms.ModelForm):
    """
    Mirrors the API's StaffSerializer: `pin` is write-only — the model only
    keeps `pin_hash`, which must never be displayed or hand-edited — required
    when creating, optional when editing, and unique within the branch.

    Without this form, staff created here would have an unusable login:
    `pin_hash` is a plain CharField with no default, so skipping it stores an
    empty hash that no PIN can ever match.
    """

    pin = forms.CharField(
        label="PIN",
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Hashed on save; the stored hash is never shown.",
    )

    class Meta:
        model = Staff
        # pin_hash is deliberately absent — see the class docstring.
        fields = ["branch", "name", "role", "is_active"]

    def clean_pin(self):
        pin = self.cleaned_data.get("pin", "")
        if not pin:
            if self.instance.pk is None:
                raise forms.ValidationError("A PIN is required when creating a staff member.")
            return ""
        # 4 characters is the minimum the admin UI itself enforces
        # (NewStaffPage.tsx); the API accepts any non-empty string, so
        # nothing stricter than this belongs here either.
        if len(pin) < 4:
            raise forms.ValidationError("Use at least 4 characters.")
        branch = self.cleaned_data.get("branch") or getattr(self.instance, "branch", None)
        if branch and Staff.pin_taken_in_branch(branch, pin, exclude_pk=self.instance.pk):
            raise forms.ValidationError("That PIN is already used by someone else in this branch.")
        return pin

    def save(self, commit=True):
        staff = super().save(commit=False)
        pin = self.cleaned_data.get("pin")
        if pin:
            staff.set_pin(pin)
        if commit:
            staff.save()
        return staff


class StaffHasPinFilter(admin.SimpleListFilter):
    """
    `pin_hash` is a salted hash, so it can't be searched or filtered on
    directly — the only question worth asking is whether a staff member can log
    in at all, i.e. whether a PIN was ever set.
    """

    title = "PIN set"
    parameter_name = "has_pin"

    def lookups(self, request, model_admin):
        return (("yes", "Yes"), ("no", "No — cannot log in"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(pin_hash="")
        if self.value() == "no":
            return queryset.filter(pin_hash="")
        return queryset


class ShiftInline(admin.TabularInline):
    """Clock-in history. Editable, because correcting a forgotten clock-out is
    a real back-office job."""

    model = Shift
    extra = 0
    fields = ("clock_in", "clock_out")
    ordering = ("-clock_in",)


@admin.register(Staff)
class StaffAdmin(SoftDeleteAdmin):
    """
    Restaurant staff — *not* Django's auth user. Login is PIN + JWT through
    accounts/authentication.py, so being a superuser here and being a staff
    member are unrelated on purpose, and the auth.User rows under
    "Authentication and Authorization" are a separate list entirely
    (see accounts/models.py).
    """

    form = StaffAdminForm
    list_display = ("id", "name", "role", "branch", "is_active", "has_pin", "is_deleted")
    list_filter = ("role", "is_active", "branch", StaffHasPinFilter, "is_deleted")
    search_fields = ("name", "branch__name_en", "branch__name_ar")
    list_select_related = ("branch",)
    autocomplete_fields = ("branch",)
    ordering = ("branch__name_en", "name", "id")
    inlines = [ShiftInline]
    fieldsets = (
        (
            "Identity",
            {
                "fields": ("branch", "name", "role", "is_active"),
                "description": "Only active staff of the selected branch appear on the login screen's name picker.",
            },
        ),
        (
            "PIN",
            {
                "fields": ("pin",),
                "description": "Stored only as a salted hash, and unique within the branch (checked on save).",
            },
        ),
        LIFECYCLE_FIELDSET,
    )

    @admin.display(boolean=True, description="PIN set")
    def has_pin(self, obj):
        return bool(obj.pin_hash)


@admin.register(Shift)
class ShiftAdmin(SoftDeleteAdmin):
    list_display = ("id", "staff", "clock_in", "clock_out", "hours", "is_deleted")
    list_filter = ("staff__branch", "staff__role", "is_deleted")
    search_fields = ("staff__name",)
    list_select_related = ("staff",)
    autocomplete_fields = ("staff",)
    date_hierarchy = "clock_in"
    # Newest first; "id" breaks same-second ties so pagination stays stable.
    ordering = ("-clock_in", "-id")

    @admin.display(description="hours")
    def hours(self, obj):
        """Shift length — the number anyone opening this list is after."""
        if not obj.clock_out:
            return "open"
        return round((obj.clock_out - obj.clock_in).total_seconds() / 3600, 2)
