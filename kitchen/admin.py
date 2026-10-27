from django.contrib import admin

from .models import KitchenStation


@admin.register(KitchenStation)
class KitchenStationAdmin(admin.ModelAdmin):
    list_display = ("name_en", "name_ar", "branch", "sort_order", "is_active")
    list_filter = ("branch", "is_active")
