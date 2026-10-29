from django.contrib import admin

from .models import VatArchiveExport, VatAuditLog, VatFiling, VatLockOverride, VatPayment, VatPeriod, VatRefund


class ReadOnlyAdmin(admin.ModelAdmin):
    """VAT records change only through the VAT returns API, which audits
    every action; the developer admin can look but not touch."""

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VatPeriod)
class VatPeriodAdmin(ReadOnlyAdmin):
    list_display = ("id", "branch", "environment", "frequency", "start_date", "end_date", "due_date", "status",
                    "locked")
    list_filter = ("environment", "frequency", "status", "locked")


@admin.register(VatAuditLog)
class VatAuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "branch", "action", "actor", "period", "summary")
    list_filter = ("action",)


@admin.register(VatFiling)
class VatFilingAdmin(ReadOnlyAdmin):
    list_display = ("id", "period", "filed_on", "zatca_amount", "calculated_net_due", "difference", "credit_treatment",
                    "is_current", "filed_by")
    list_filter = ("is_current", "credit_treatment")


@admin.register(VatLockOverride)
class VatLockOverrideAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "period", "action", "object_type", "object_id", "actor")
    list_filter = ("action",)


@admin.register(VatPayment)
class VatPaymentAdmin(ReadOnlyAdmin):
    list_display = ("id", "period", "amount", "paid_on", "sadad_reference", "source", "voided", "recorded_by")
    list_filter = ("voided", "source")


@admin.register(VatRefund)
class VatRefundAdmin(ReadOnlyAdmin):
    list_display = ("id", "period", "amount", "received_on", "reference", "voided", "recorded_by")
    list_filter = ("voided",)


@admin.register(VatArchiveExport)
class VatArchiveExportAdmin(ReadOnlyAdmin):
    list_display = ("id", "branch", "environment", "status", "sales_count", "purchases_count", "missing_count",
                    "requested_by", "created_at", "finished_at")
    list_filter = ("status", "environment")
