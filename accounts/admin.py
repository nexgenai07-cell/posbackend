from django import forms
from django.contrib import admin

from common.admin import LIFECYCLE_FIELDSET, SoftDeleteAdmin

from .models import Shift, Staff
from .passwords import password_error_codes
from .services import unlock_staff
from .usernames import is_valid_username, normalize_username


class StaffAdminForm(forms.ModelForm):
    """
    Mirrors the API's StaffSerializer: `password` is write-only - the model
    only keeps `password_hash`, which must never be displayed or hand-edited -
    required when creating, optional when editing, and checked against the
    same strength rules as the app (accounts/passwords.py).

    Without this form, staff created here would have an unusable login:
    an empty `password_hash` matches no password.
    """

    password = forms.CharField(
        label="Password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=(
            "Hashed on save; the stored hash is never shown. Setting one here also "
            "ticks 'must change password', since the staff member did not choose it."
        ),
    )

    class Meta:
        model = Staff
        # password_hash is deliberately absent - see the class docstring.
        fields = ["branch", "name", "username", "role", "is_active", "must_change_password"]

    def clean_username(self):
        username = normalize_username(self.cleaned_data.get("username"))
        if username and not is_valid_username(username):
            raise forms.ValidationError("3-64 characters: lowercase letters, digits, dot, underscore or hyphen.")
        # Blank is allowed: Staff.save() generates one from the name.
        return username

    def clean_password(self):
        password = self.cleaned_data.get("password", "")
        if not password:
            if self.instance.pk is None:
                raise forms.ValidationError("A password is required when creating a staff member.")
            return ""
        probe = Staff(name=self.cleaned_data.get("name", ""), username=self.cleaned_data.get("username", ""))
        codes = password_error_codes(password, probe)
        if codes:
            raise forms.ValidationError(
                "Use at least 8 characters, not all digits, not a common password, and not your name or username."
            )
        return password

    def save(self, commit=True):
        staff = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            staff.set_password(password)
            staff.must_change_password = True
        if commit:
            staff.save()
        return staff


class StaffLockedFilter(admin.SimpleListFilter):
    title = "locked"
    parameter_name = "locked"

    def lookups(self, request, model_admin):
        return (("yes", "Locked"), ("no", "Not locked"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(locked_at__isnull=False)
        if self.value() == "no":
            return queryset.filter(locked_at__isnull=True)
        return queryset


class StaffHasPasswordFilter(admin.SimpleListFilter):
    """
    `password_hash` is a salted hash, so it can't be searched or filtered on
    directly - the only question worth asking is whether a staff member can
    log in at all, i.e. whether a password was ever set.
    """

    title = "password set"
    parameter_name = "has_password"

    def lookups(self, request, model_admin):
        return (("yes", "Yes"), ("no", "No - cannot log in"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(password_hash="")
        if self.value() == "no":
            return queryset.filter(password_hash="")
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
    Restaurant staff - *not* Django's auth user. Login is username + password
    + JWT through accounts/authentication.py, so being a superuser here and
    being a staff member are unrelated on purpose, and the auth.User rows
    under "Authentication and Authorization" are a separate list entirely
    (see accounts/models.py).

    This is also the developer-side recovery path when the Owner locks
    themself out: the "Unlock selected" action below, or
    `python manage.py unlock_staff <username>`.
    """

    form = StaffAdminForm
    list_display = ("id", "name", "username", "role", "branch", "is_active", "is_locked", "has_password", "is_deleted")
    list_filter = ("role", "is_active", StaffLockedFilter, "branch", StaffHasPasswordFilter, "is_deleted")
    search_fields = ("name", "username", "branch__name_en", "branch__name_ar")
    list_select_related = ("branch",)
    autocomplete_fields = ("branch",)
    ordering = ("branch__name_en", "name", "id")
    inlines = [ShiftInline]
    actions = SoftDeleteAdmin.actions + ("unlock_selected",)
    readonly_fields = SoftDeleteAdmin.readonly_fields + (
        "failed_login_attempts", "locked_at", "locked_reason", "unlocked_by", "unlocked_at",
    )
    fieldsets = (
        (
            "Identity",
            {
                "fields": ("branch", "name", "username", "role", "is_active"),
                "description": "Username is what the staff member types on the login screen. Leave it blank to generate one from the name.",
            },
        ),
        (
            "Password",
            {
                "fields": ("password", "must_change_password"),
                "description": "Stored only as a salted hash. A password set here is temporary: the staff member must replace it on their next login.",
            },
        ),
        (
            "Lockout",
            {
                "fields": ("failed_login_attempts", "locked_at", "locked_reason", "unlocked_by", "unlocked_at"),
                "description": "Three wrong passwords in a row lock the account. Clear it with the 'Unlock selected' action on the list page.",
            },
        ),
        LIFECYCLE_FIELDSET,
    )

    @admin.display(boolean=True, description="locked")
    def is_locked(self, obj):
        return obj.is_locked

    @admin.display(boolean=True, description="password set")
    def has_password(self, obj):
        return bool(obj.password_hash)

    @admin.action(description="Unlock selected (clear lock and failed-login counter)")
    def unlock_selected(self, request, queryset):
        count = 0
        for staff in queryset.filter(locked_at__isnull=False):
            unlock_staff(staff, by=None)
            count += 1
        self.message_user(request, f"Unlocked {count} account(s).")


@admin.register(Shift)
class ShiftAdmin(SoftDeleteAdmin):
    list_display = ("id", "staff", "clock_in", "clock_out", "hours", "is_deleted")
    list_filter = ("staff__branch", "staff__role", "is_deleted")
    search_fields = ("staff__name", "staff__username")
    list_select_related = ("staff",)
    autocomplete_fields = ("staff",)
    date_hierarchy = "clock_in"
    # Newest first; "id" breaks same-second ties so pagination stays stable.
    ordering = ("-clock_in", "-id")

    @admin.display(description="hours")
    def hours(self, obj):
        """Shift length - the number anyone opening this list is after."""
        if not obj.clock_out:
            return "open"
        return round((obj.clock_out - obj.clock_in).total_seconds() / 3600, 2)
