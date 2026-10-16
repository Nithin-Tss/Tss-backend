from django.contrib import admin
from .models import ( Location, InventoryItem, Transfer, TransferItem, InventoryHistory )

# Registering models
admin.site.register(Location)
admin.site.register(InventoryItem)
admin.site.register(Transfer)
admin.site.register(TransferItem)


@admin.register(InventoryHistory)
class InventoryHistoryAdmin(admin.ModelAdmin):
    """Stock history is an audit trail: it can be viewed here, never added, edited or deleted."""

    list_display = ("created_at", "variant", "location", "change", "after", "reason", "source", "user")
    list_filter = ("reason", "source")
    search_fields = ("variant__sku", "variant__product__title", "reference_id", "note")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
