from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Wishlist, WishlistItem


@admin.register(Wishlist)
class WishlistAdmin(StoreGuardAdmin):
    store_links = {"customer": "store"}


@admin.register(WishlistItem)
class WishlistItemAdmin(StoreGuardAdmin):
    store_path = "wishlist__store"
    parent_fields = ("wishlist",)
    store_links = {"wishlist": "store", "product": "store", "variant": "product.store"}
