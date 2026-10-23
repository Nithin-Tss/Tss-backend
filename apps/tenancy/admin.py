import logging

from django.contrib import admin, messages
from django.db.models import Prefetch

from apps.core.admin_guards import StoreGuardAdmin
from .models import Store, StoreMembership
from .services import OWNER, REMOVED, purge_store


logger = logging.getLogger(__name__)


def owner_memberships():
    """A store's current owners: owner memberships that were not removed (a closed store's still count)."""
    return StoreMembership.objects.filter(role=OWNER).exclude(status=REMOVED)


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("store_name", "store_slug", "owner_emails", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("store_name", "store_slug")

    def get_queryset(self, request):
        owners = owner_memberships().select_related("user").order_by("created_at")
        return super().get_queryset(request).prefetch_related(
            Prefetch("memberships", queryset=owners, to_attr="owner_memberships")
        )

    @admin.display(description="Owner email")
    def owner_emails(self, obj):
        # A store may have several owners, and its creator may no longer be one of them
        return ", ".join(m.user.email for m in obj.owner_memberships) or "-"

    def get_search_results(self, request, queryset, search_term):
        results, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        term = search_term.strip()
        if term:
            # Owners only: a staff member's email must not find the stores they merely work in
            owned = owner_memberships().filter(user__email__icontains=term).values("store_id")
            results = results | queryset.filter(pk__in=owned)
        return results, may_have_duplicates

    def get_deleted_objects(self, objs, request):
        # PROTECT links would block the delete page; deleting a store removes all of its data instead
        names = ", ".join(str(o) for o in objs)
        return ([f"Store: {names} and ALL of its data (members, products, orders, themes, ...)"], {}, set(), [])

    def delete_model(self, request, obj):
        self._purge(request, obj)

    def delete_queryset(self, request, queryset):
        for store in queryset:
            self._purge(request, store)

    def _purge(self, request, store):
        # Each store is its own transaction; a failure is reported on the page (and logged)
        # instead of turning the whole request into a bare "Server Error (500)".
        try:
            purge_store(store)
        except Exception as error:
            logger.exception("Could not delete store %s", store.pk)
            messages.error(request, f'Could not delete store "{store}": {type(error).__name__}: {error}')


@admin.register(StoreMembership)
class StoreMembershipAdmin(StoreGuardAdmin):
    list_display = ("user", "store_name", "role", "status")
    search_fields = ("user__email", "store__store_name")
