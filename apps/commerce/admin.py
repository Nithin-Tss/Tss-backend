from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Cart, CartItem, Checkout, CheckoutItem


@admin.register(Cart)
class CartAdmin(StoreGuardAdmin):
    store_links = {"customer": "store"}


@admin.register(CartItem)
class CartItemAdmin(StoreGuardAdmin):
    store_path = "cart__store"
    parent_fields = ("cart",)
    store_links = {"cart": "store", "product": "store", "variant": "product.store"}


@admin.register(Checkout)
class CheckoutAdmin(StoreGuardAdmin):
    store_links = {"cart": "store", "customer": "store"}


@admin.register(CheckoutItem)
class CheckoutItemAdmin(StoreGuardAdmin):
    store_path = "checkout__store"
    parent_fields = ("checkout",)
    store_links = {"checkout": "store", "product": "store", "variant": "product.store"}
