"""Django Admin guard for store-owned data: no record may reference another store's records,
and a record's store (or its parent record) cannot be changed once created.

Admin subclasses set:
  store_path    lookup from the model to its Store (default "store")
  parent_fields foreign keys that fix which store the row belongs to (read-only after create)
  store_links   {field: dotted path from the linked record to its store} that must all match
"""
from django.contrib import admin
from django.core.exceptions import ValidationError


def _resolve(obj, dotted):
    for part in dotted.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


class StoreGuardAdmin(admin.ModelAdmin):
    store_path = "store"
    parent_fields = ()
    store_links = {}

    @admin.display(description="Store", ordering="store__store_name")
    def store_name(self, obj):
        store = _resolve(obj, self.store_path.replace("__", "."))
        return store.store_name if store else "-"

    def get_list_filter(self, request):
        return (*super().get_list_filter(request), self.store_path)

    def get_list_select_related(self, request):
        return True if self.list_select_related is False else self.list_select_related

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            locked = ["store"] if self.store_path == "store" else []
            fields += [f for f in (*locked, *self.parent_fields) if f not in fields]
        return fields

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change=change, **kwargs)
        links = self.store_links
        direct = self.store_path == "store"

        class GuardedForm(form):
            def clean(self):
                cleaned = super().clean()
                instance = self.instance

                def value(name):
                    if name in self.fields and name in self.cleaned_data:
                        return self.cleaned_data[name]
                    return getattr(instance, name, None) if instance.pk else None

                own = value("store") if direct else None
                reference = own.pk if own else None
                for field, path in links.items():
                    store = _resolve(value(field), path)
                    if store is None:
                        continue
                    if reference is None:
                        reference = store.pk
                    elif store.pk != reference and field in self.fields:
                        self.add_error(field, ValidationError(
                            "This belongs to a different store.", code="store_mismatch"))
                return cleaned

        return GuardedForm
