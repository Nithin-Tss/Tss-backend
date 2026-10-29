from django.contrib import admin
from django.core.exceptions import ValidationError

from apps.core.admin_guards import SameStoreForm, StoreGuardAdmin
from .models import ( Location, InventoryItem, Transfer, TransferItem, InventoryHistory, StockReservation )

# Transfers that already moved their stock (or never will): their record stays as it is
FINISHED = ("completed", "cancelled")


@admin.register(Location)
class LocationAdmin(StoreGuardAdmin):
    pass


@admin.register(InventoryItem)
class InventoryItemAdmin(StoreGuardAdmin):
    """
    Stock levels can be viewed here, never added, edited or deleted: every change goes through
    inventory.services.adjust_inventory (Inventory API), which locks the row, checks the numbers and
    writes the history.
    """

    store_path = "location__store"
    parent_fields = ("variant", "location")
    store_links = {"location": "store", "variant": "product.store"}

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Transfer)
class TransferAdmin(StoreGuardAdmin):
    """
    The status is never edited here: only the transfer's complete/cancel actions (Inventory API) change
    it, so a transfer can't be marked completed without its stock moving. New transfers start pending;
    completed and cancelled ones can't be changed or deleted.
    """

    store_links = {"source_location": "store", "destination_location": "store"}

    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) + ("status",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.status in FINISHED)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.status in FINISHED)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.status = "pending"
        super().save_model(request, obj, form, change)


class TransferItemForm(SameStoreForm):
    def clean(self):
        cleaned = super().clean()
        transfer = self._value("transfer")
        if transfer is not None and transfer.status in FINISHED:
            raise ValidationError(f"This transfer is already {transfer.status}; its items can't change.")
        return cleaned


@admin.register(TransferItem)
class TransferItemAdmin(StoreGuardAdmin):
    """Items of a completed or cancelled transfer can't be added, changed or deleted."""

    form = TransferItemForm
    store_path = "transfer__store"
    parent_fields = ("transfer",)
    store_links = {"transfer": "store", "variant": "product.store"}

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.transfer.status in FINISHED)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.transfer.status in FINISHED)


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


@admin.register(StockReservation)
class StockReservationAdmin(StoreGuardAdmin):
    """Checkout stock holds are made and ended by the checkout flow: viewable here, never added, edited or deleted."""

    list_display = ("created_at", "store_name", "variant", "quantity", "status", "expires_at", "checkout_id")
    list_filter = ("status",)
    search_fields = ("variant__sku", "variant__product__title", "checkout_id")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
