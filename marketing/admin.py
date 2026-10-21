from django.contrib import admin

from .models import Campaign, CampaignPost, CampaignRecipient, SocialConnection


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


class CampaignPostInline(admin.TabularInline):
    model = CampaignPost
    extra = 0
    fields = ("channel", "status", "permalink", "error", "attempts", "posted_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # Posts are created by publishing, which is the only thing that
        # actually puts them on the Page.
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "status", "send_whatsapp", "post_facebook", "post_instagram", "sent_at")
    list_filter = ("status", "send_whatsapp", "post_facebook", "post_instagram", "branch")
    search_fields = ("name", "message_en", "message_ar", "post_caption")
    inlines = [CampaignPostInline, CampaignRecipientInline]


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = ("phone", "name", "campaign", "status", "sent_at")
    list_filter = ("status", "campaign__branch")
    search_fields = ("phone", "name")


@admin.register(SocialConnection)
class SocialConnectionAdmin(admin.ModelAdmin):
    list_display = ("branch", "provider", "page_name", "ig_username", "status", "last_checked_at")
    list_filter = ("provider", "status")
    # The encrypted token is left off the form entirely: pasting a new one
    # here would skip the Meta check and the encryption in marketing.social.
    exclude = ("access_token_enc",)
    readonly_fields = ("connected_by", "connected_at", "last_checked_at", "last_error")
