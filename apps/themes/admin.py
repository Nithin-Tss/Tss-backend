from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Theme, ThemeFile, StoreTheme


# Theme and ThemeFile are the platform's shared theme library, not owned by a store
admin.site.register(Theme)
admin.site.register(ThemeFile)


@admin.register(StoreTheme)
class StoreThemeAdmin(StoreGuardAdmin):
    list_display = ("__str__", "store_name", "theme", "is_active")
