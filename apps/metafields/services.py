"""Metafields and metaobjects: who a value can belong to, and the shape of a metaobject."""
from rest_framework.exceptions import ValidationError

from apps.catalog.models import Brand, Collection, Product, ProductVariant

from .models import OWNER_TYPES, VALUE_TYPES
from .types import clean_value

_OWNER_QUERYSETS = {
    "product": lambda store: Product.objects.filter(store=store),
    "variant": lambda store: ProductVariant.objects.filter(product__store=store),
    "collection": lambda store: Collection.objects.filter(store=store),
    "brand": lambda store: Brand.objects.filter(store=store),
}
assert set(_OWNER_QUERYSETS) == set(OWNER_TYPES)


def owner_exists(store, owner_type, owner_id):
    """Whether `owner_id` is a live resource of this kind in this store."""
    qs = _OWNER_QUERYSETS[owner_type](store)
    if any(f.name == "is_deleted" for f in qs.model._meta.concrete_fields):
        qs = qs.filter(is_deleted=False)
    return qs.filter(pk=owner_id).exists()


def validate_field_list(fields):
    """Check the field list of a metaobject definition and return it cleaned."""
    import re

    if not isinstance(fields, list) or not fields:
        raise ValidationError("Add at least one field.")
    cleaned, seen = [], set()
    for i, field in enumerate(fields, 1):
        if not isinstance(field, dict):
            raise ValidationError(f"Field {i} must be an object.")
        key, type_ = field.get("key"), field.get("type")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise ValidationError(f"Field {i}: the key must be lowercase letters, numbers and underscores.")
        if key in seen:
            raise ValidationError(f"Field {i}: the key '{key}' is used twice.")
        if type_ not in VALUE_TYPES:
            raise ValidationError(f"Field {i}: unknown type '{type_}'.")
        validations = field.get("validations") or {}
        if not isinstance(validations, dict):
            raise ValidationError(f"Field {i}: validations must be an object.")
        seen.add(key)
        cleaned.append({
            "key": key,
            "name": str(field.get("name") or key),
            "type": type_,
            "required": bool(field.get("required", False)),
            "validations": validations,
        })
    return cleaned


def clean_metaobject_values(definition, values, store):
    """Check an entry's values against the definition's fields; unknown keys are refused."""
    if not isinstance(values, dict):
        raise ValidationError({"values": "Must be an object of field key to value."})
    by_key = {f["key"]: f for f in definition.fields}
    errors = {}
    for key in values:
        if key not in by_key:
            errors[key] = "Not a field of this metaobject type."
    cleaned = {}
    for key, field in by_key.items():
        value = values.get(key)
        if value in (None, ""):
            if field.get("required"):
                errors[key] = "This field is required."
            continue
        try:
            cleaned[key] = clean_value(field["type"], value, field.get("validations"), store)
        except ValidationError as e:
            errors[key] = e.detail
    if errors:
        raise ValidationError({"values": errors})
    return cleaned
