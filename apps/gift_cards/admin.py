from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import GiftCard, GiftCardTransaction


@admin.register(GiftCard)
class GiftCardAdmin(StoreGuardAdmin):
    store_links = {"customer": "store"}


@admin.register(GiftCardTransaction)
class GiftCardTransactionAdmin(StoreGuardAdmin):
    """Gift card transactions are the balance ledger: they can be viewed here, never added, edited or deleted."""

    store_path = "gift_card__store"
    parent_fields = ("gift_card",)
    store_links = {"gift_card": "store", "order": "store", "checkout": "store", "payment": "store"}

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
