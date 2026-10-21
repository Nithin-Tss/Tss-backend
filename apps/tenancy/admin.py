from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Store, StoreMembership


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("store_name", "store_slug", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("store_name", "store_slug")


@admin.register(StoreMembership)
class StoreMembershipAdmin(StoreGuardAdmin):
    list_display = ("user", "store_name", "role", "status")
    search_fields = ("user__email", "store__store_name")
