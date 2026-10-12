from django.contrib import admin

from .models import Review


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "rating", "source", "status", "posted_at")
    list_filter = ("status", "source", "rating")
    search_fields = ("name", "comment")
