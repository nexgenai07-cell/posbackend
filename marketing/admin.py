from django.contrib import admin

from .models import Campaign, CampaignRecipient


class CampaignRecipientInline(admin.TabularInline):
    model = CampaignRecipient
    extra = 0
    fields = ("phone", "name", "status", "sent_at", "error")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        # Recipients are a frozen snapshot built by marketing/services.py.
        # Adding one by hand would produce a row with no rendered message.
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "channel", "status", "prepared_at", "sent_at")
    list_filter = ("status", "channel", "branch")
    search_fields = ("name", "message_en", "message_ar")
    inlines = [CampaignRecipientInline]


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = ("phone", "name", "campaign", "status", "sent_at")
    list_filter = ("status", "campaign__branch")
    search_fields = ("phone", "name")
