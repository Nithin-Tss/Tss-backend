from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import GiftCard, GiftCardTransaction


@admin.register(GiftCard)
class GiftCardAdmin(StoreGuardAdmin):
    store_links = {"customer": "store"}


@admin.register(GiftCardTransaction)
class GiftCardTransactionAdmin(StoreGuardAdmin):
    store_path = "gift_card__store"
    parent_fields = ("gift_card",)
    store_links = {"gift_card": "store", "order": "store", "checkout": "store", "payment": "store"}
