from django.contrib import admin

from .models import ThemeFile


@admin.register(ThemeFile)
class ThemeFileAdmin(admin.ModelAdmin):
    list_display = ("path", "store", "updated_at")
    search_fields = ("path",)
