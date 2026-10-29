import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.db.models import Prefetch

from apps.core.admin_guards import SameStoreForm, StoreGuardAdmin
from .models import ShippingRate, Store, StoreMembership, TaxRate
from .services import ACTIVE, OWNER, REMOVED, purge_store


logger = logging.getLogger(__name__)


def owner_memberships():
    """A store's current owners: owner memberships that were not removed (a closed store's still count)."""
    return StoreMembership.objects.filter(role=OWNER).exclude(status=REMOVED)


class StoreAddForm(forms.ModelForm):
    """Adding a store in Django Admin: it needs an owner, an existing account picked by email."""

    owner_email = forms.EmailField(help_text="Email of an existing account; it becomes the store's owner.")

    class Meta:
        model = Store
        fields = "__all__"

    def clean_owner_email(self):
        email = self.cleaned_data["owner_email"].strip()
        self.owner = get_user_model().objects.filter(email__iexact=email).first()
        if self.owner is None:
            raise forms.ValidationError("No account has this email.")
        return email


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("store_name", "store_slug", "owner_emails", "created_by", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("store_name", "store_slug")
    readonly_fields = ("created_by",)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = StoreAddForm
        return super().get_form(request, obj, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user.email
        super().save_model(request, obj, form, change)
        if not change:
            # Same as the API's create_store; the admin already runs this in one transaction
            from .access import ensure_built_in_roles
            StoreMembership.objects.create(user=form.owner, store=obj, role=OWNER, status=ACTIVE)
            ensure_built_in_roles(obj)

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


LAST_OWNER = "the last owner of {store}; make someone else its owner first"


def leaves_no_owner(store_id, without_pks):
    """True when the store has no owner left once these memberships are gone or no longer owners."""
    return not owner_memberships().filter(store_id=store_id).exclude(pk__in=without_pks).exists()


class StoreMembershipForm(SameStoreForm):
    def clean(self):
        cleaned = super().clean()
        old = StoreMembership.objects.filter(pk=self.instance.pk).first()
        stops_owning = cleaned.get("role") != OWNER or cleaned.get("status") == REMOVED
        if old and old.role == OWNER and old.status != REMOVED and stops_owning \
                and leaves_no_owner(old.store_id, [old.pk]):
            raise forms.ValidationError(f"This is {LAST_OWNER.format(store=old.store)}.")
        return cleaned


@admin.register(StoreMembership)
class StoreMembershipAdmin(StoreGuardAdmin):
    form = StoreMembershipForm
    list_display = ("user", "store_name", "role", "status")
    search_fields = ("user__email", "store__store_name")

    def get_deleted_objects(self, objs, request):
        # Listed as protected, so both the delete page and the bulk action refuse
        to_delete, counts, perms_needed, protected = super().get_deleted_objects(objs, request)
        pks = [o.pk for o in objs]
        for m in objs:
            if m.role == OWNER and m.status != REMOVED and leaves_no_owner(m.store_id, pks):
                protected.append(f"{m}: {LAST_OWNER.format(store=m.store)}")
        return to_delete, counts, perms_needed, protected


@admin.register(ShippingRate)
class ShippingRateAdmin(StoreGuardAdmin):
    list_display = ("__str__", "store_name", "country", "min_weight", "max_weight", "price")
    search_fields = ("name", "country", "store__store_name")


@admin.register(TaxRate)
class TaxRateAdmin(StoreGuardAdmin):
    list_display = ("__str__", "store_name", "country", "region", "rate")
    search_fields = ("name", "country", "region", "store__store_name")
