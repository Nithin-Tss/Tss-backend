from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Payment, PaymentTransaction


@admin.register(Payment)
class PaymentAdmin(StoreGuardAdmin):
    store_links = {"checkout": "store", "order": "store", "customer": "store"}


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(StoreGuardAdmin):
    """Payment transactions are a ledger: they can be viewed here, never added, edited or deleted."""

    store_path = "payment__store"
    parent_fields = ("payment",)
    store_links = {"payment": "store"}

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
