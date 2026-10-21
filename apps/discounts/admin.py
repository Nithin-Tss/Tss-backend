from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import ( Discount, DiscountProduct, DiscountCode, OrderDiscount, )


@admin.register(Discount)
class DiscountAdmin(StoreGuardAdmin):
    pass


@admin.register(DiscountProduct)
class DiscountProductAdmin(StoreGuardAdmin):
    store_path = "discount__store"
    store_links = {"discount": "store", "product": "store"}


@admin.register(DiscountCode)
class DiscountCodeAdmin(StoreGuardAdmin):
    store_path = "discount__store"
    parent_fields = ("discount",)
    store_links = {"discount": "store"}


@admin.register(OrderDiscount)
class OrderDiscountAdmin(StoreGuardAdmin):
    store_path = "order__store"
    store_links = {"order": "store", "discount": "store", "discount_code": "discount.store"}
