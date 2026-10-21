"""Django Admin helpers for store-owned records (shared by the app admin modules)."""
from operator import attrgetter

from django import forms
from django.contrib import admin


class SameStoreForm(forms.ModelForm):
    """Rejects a record whose linked objects belong to different stores.

    store_links maps a field to the path of its object's store, e.g.
    {"category": "store"}; the record's own store (if it has one) is checked too.
    """

    store_links = {}

    def _value(self, name):
        # A field missing from the form is read-only here: keep the saved value.
        if name in self.fields:
            return self.cleaned_data.get(name)
        if getattr(self.instance, name + "_id", None) is None:
            return None
        return getattr(self.instance, name)

    def clean(self):
        cleaned = super().clean()
        anchor = anchor_field = None
        if any(f.name == "store" for f in self._meta.model._meta.fields):
            anchor, anchor_field = self._value("store"), "store"
        for name, path in self.store_links.items():
            obj = self._value(name)
            if obj is None:
                continue
            store = attrgetter(path)(obj)
            if anchor is None:
                anchor, anchor_field = store, name
            elif store.pk != anchor.pk:
                message = (f"{obj} belongs to store \"{store}\", but the "
                           f"{anchor_field.replace('_', ' ')} belongs to store \"{anchor}\".")
                self.add_error(name if name in self.fields else None, message)
        return cleaned


class StoreGuardAdmin(admin.ModelAdmin):
    """Admin for store-owned records.

    Shows and filters by store, keeps the owning store (and parent_fields) fixed
    once a record exists, and validates store_links on every save.
    """

    form = SameStoreForm
    store_links = {}
    parent_fields = ()     # fields that fix the record's store, read-only after creation
    store_path = "store"   # lookup from this model to its Store, for the list column/filter
    list_display = ("__str__", "store_name")

    def get_list_filter(self, request):
        return (self.store_path,) + tuple(super().get_list_filter(request))

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(self.store_path)

    @admin.display(description="Store")
    def store_name(self, obj):
        return attrgetter(self.store_path.replace("__", "."))(obj).store_name

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly += ("store",) if self.store_path == "store" else ()
            readonly += tuple(self.parent_fields)
        return readonly

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.store_links = self.store_links
        return form
