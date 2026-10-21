from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Payment, PaymentTransaction


@admin.register(Payment)
class PaymentAdmin(StoreGuardAdmin):
    store_links = {"checkout": "store", "order": "store", "customer": "store"}


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(StoreGuardAdmin):
    store_path = "payment__store"
    parent_fields = ("payment",)
    store_links = {"payment": "store"}
