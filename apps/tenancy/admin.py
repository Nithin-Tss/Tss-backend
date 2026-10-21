from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Store, StoreMembership
from .services import purge_store


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("store_name", "store_slug", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("store_name", "store_slug")

    def get_deleted_objects(self, objs, request):
        # PROTECT links would block the delete page; deleting a store removes all of its data instead
        names = ", ".join(str(o) for o in objs)
        return ([f"Store: {names} and ALL of its data (members, products, orders, themes, ...)"], {}, set(), [])

    def delete_model(self, request, obj):
        purge_store(obj)

    def delete_queryset(self, request, queryset):
        for store in queryset:
            purge_store(store)


@admin.register(StoreMembership)
class StoreMembershipAdmin(StoreGuardAdmin):
    list_display = ("user", "store_name", "role", "status")
    search_fields = ("user__email", "store__store_name")
