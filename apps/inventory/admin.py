from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import ( Location, InventoryItem, Transfer, TransferItem, InventoryHistory )


@admin.register(Location)
class LocationAdmin(StoreGuardAdmin):
    pass


@admin.register(InventoryItem)
class InventoryItemAdmin(StoreGuardAdmin):
    store_path = "location__store"
    parent_fields = ("variant", "location")
    store_links = {"location": "store", "variant": "product.store"}


@admin.register(Transfer)
class TransferAdmin(StoreGuardAdmin):
    store_links = {"source_location": "store", "destination_location": "store"}


@admin.register(TransferItem)
class TransferItemAdmin(StoreGuardAdmin):
    store_path = "transfer__store"
    parent_fields = ("transfer",)
    store_links = {"transfer": "store", "variant": "product.store"}


@admin.register(InventoryHistory)
class InventoryHistoryAdmin(StoreGuardAdmin):
    """Stock history is an audit trail: it can be viewed here, never added, edited or deleted."""

    list_display = ("created_at", "store_name", "variant", "location", "change", "after", "reason", "source", "user")
    list_filter = ("reason", "source")
    search_fields = ("variant__sku", "variant__product__title", "reference_id", "note")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
