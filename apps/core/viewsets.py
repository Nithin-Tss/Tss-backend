from functools import lru_cache

from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from apps.tenancy.models import Store

from .permissions import IsStoreMember


@lru_cache(maxsize=None)
def store_path(model):
    """
    The lookup from `model` to its Store, following foreign keys:
    Order -> "store", OrderItem -> "order__store",
    InventoryItem -> "location__store". None if there is no path.
    """
    if model is Store:
        return "pk"

    queue = [(model, "")]
    seen = {model}

    while queue:
        current, prefix = queue.pop(0)
        for field in current._meta.concrete_fields:
            if not field.many_to_one:
                continue
            if field.related_model is Store:
                return prefix + field.name
            if field.related_model not in seen and prefix.count("__") < 3:
                seen.add(field.related_model)
                queue.append((field.related_model, f"{prefix}{field.name}__"))

    return None


def has_field(model, name):
    return any(f.name == name for f in model._meta.concrete_fields)


class StoreScopedViewSet(viewsets.ModelViewSet):
    """
    Base viewset for every store-owned resource.

    - Only rows of the current store are visible (found via store_path, or
      set `store_lookup` by hand).
    - Soft-deleted rows (is_deleted=True) are hidden; DELETE soft-deletes
      when the model supports it.
    - New rows get the current store and is_deleted=False automatically.
    """

    permission_classes = [IsAuthenticated, IsStoreMember]
    store_lookup = None

    def get_queryset(self):
        qs = super().get_queryset()
        lookup = self.store_lookup or store_path(qs.model)
        qs = qs.filter(**{lookup: self.request.store})

        if has_field(qs.model, "is_deleted"):
            qs = qs.filter(is_deleted=False)

        return qs

    def creation_defaults(self, model):
        extra = {}
        if has_field(model, "store"):
            extra["store"] = self.request.store
        if has_field(model, "is_deleted"):
            extra["is_deleted"] = False
        return extra

    def perform_create(self, serializer):
        serializer.save(**self.creation_defaults(serializer.Meta.model))

    def perform_destroy(self, instance):
        if has_field(type(instance), "is_deleted"):
            instance.is_deleted = True
            instance.deleted_at = timezone.now()
            instance.save(update_fields=["is_deleted", "deleted_at"])
        else:
            try:
                instance.delete()
            except ProtectedError:
                raise ValidationError("This is still used by other records, so it can't be deleted.")


class StoreScopedSerializerMixin:
    """
    Limits every related-object field to the current store - including
    models that only reach the store through a parent (a variant through its
    product) - so a request for store A can't point at store B's data.
    """

    def get_fields(self):
        fields = super().get_fields()
        store = getattr(self.context.get("request"), "store", None)

        if store is None:
            return fields

        for field in fields.values():
            relation = getattr(field, "child_relation", field)
            queryset = getattr(relation, "queryset", None)

            if queryset is None:
                continue

            lookup = store_path(queryset.model)
            if lookup:
                queryset = queryset.filter(**{lookup: store})
            if has_field(queryset.model, "is_deleted"):
                queryset = queryset.filter(is_deleted=False)
            relation.queryset = queryset

        return fields
