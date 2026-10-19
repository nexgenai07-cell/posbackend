from django.contrib import admin

from .models import ZatcaAuditLog, ZatcaDevice, ZatcaDocument, ZatcaGoLive, ZatcaSettings


@admin.register(ZatcaSettings)
class ZatcaSettingsAdmin(admin.ModelAdmin):
    """Read-mostly view for developers; owners edit these on the ZATCA page.
    `environment` is read-only here too: it changes only through the go-live
    wizard, which also creates the new device for that environment."""

    list_display = ("branch", "environment", "enabled", "vat_number", "seller_name_en", "updated_at")
    list_filter = ("environment", "enabled")
    readonly_fields = ("environment", "created_at", "updated_at")


@admin.register(ZatcaDevice)
class ZatcaDeviceAdmin(admin.ModelAdmin):
    """Fully read-only, and the encrypted *_enc columns are left out
    altogether, so a developer login can't see a secret even as ciphertext.
    Devices change only through onboarding, import and go-live."""

    list_display = ("id", "branch", "environment", "status", "is_active", "common_name", "last_icv", "created_at")
    list_filter = ("environment", "status", "is_active")
    fields = (
        "branch", "environment", "status", "is_active", "egs_serial", "common_name", "location",
        "invoice_types", "csr", "compliance_cert", "production_cert", "secrets_set_at",
        "last_icv", "last_hash", "invoice_seq", "credit_seq", "debit_seq", "archived_at", "created_at", "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ZatcaAuditLog)
class ZatcaAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "branch", "action", "ok", "actor", "actor_label", "device")
    list_filter = ("action", "ok")
    readonly_fields = ("branch", "device", "actor", "actor_label", "action", "ok", "summary", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ZatcaDocument)
class ZatcaDocumentAdmin(admin.ModelAdmin):
    """Read-only: a signed document is never edited, only answered by ZATCA."""

    list_display = ("number", "branch", "environment", "kind", "simplified", "icv", "status", "zatca_status", "total",
                    "issued_at")
    list_filter = ("environment", "status", "kind", "simplified", "source")
    search_fields = ("number", "uuid")

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ZatcaGoLive)
class ZatcaGoLiveAdmin(admin.ModelAdmin):
    """Read-only history of go-live wizard runs. Holds no OTP or secret:
    only the business details, the check results and who did what."""

    list_display = ("id", "branch", "environment", "status", "activated", "device", "started_by", "created_at")
    list_filter = ("environment", "status", "activated")
    readonly_fields = ("branch", "environment", "status", "details", "device", "checks", "legal_confirmed_at",
                       "activated", "started_by", "created_at", "updated_at", "finished_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
