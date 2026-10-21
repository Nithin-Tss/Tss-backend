from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Customer, CustomerAddress


@admin.register(Customer)
class CustomerAdmin(StoreGuardAdmin):
    pass


@admin.register(CustomerAddress)
class CustomerAddressAdmin(StoreGuardAdmin):
    store_path = "customer__store"
    parent_fields = ("customer",)
    store_links = {"customer": "store"}
