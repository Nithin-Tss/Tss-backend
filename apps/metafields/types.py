"""Checking and cleaning a value against a field type and its validations."""
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework.exceptions import ValidationError

_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _number_limits(value, rules):
    if "min" in rules and value < rules["min"]:
        raise ValidationError(f"Must be at least {rules['min']}.")
    if "max" in rules and value > rules["max"]:
        raise ValidationError(f"Must be at most {rules['max']}.")


def _text(value, rules, multiline):
    if not isinstance(value, str):
        raise ValidationError("Must be text.")
    if not multiline and "\n" in value:
        raise ValidationError("Must be a single line.")
    if "max_length" in rules and len(value) > rules["max_length"]:
        raise ValidationError(f"Must be at most {rules['max_length']} characters.")
    if rules.get("choices") and value not in rules["choices"]:
        raise ValidationError("Must be one of: " + ", ".join(map(str, rules["choices"])) + ".")
    return value


def clean_value(type_, value, rules=None, store=None):
    """Return `value` in its stored (JSON) form, or raise a DRF ValidationError."""
    rules = rules or {}

    if type_ == "single_line_text":
        return _text(value, rules, False)
    if type_ == "multi_line_text":
        return _text(value, rules, True)

    if type_ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("Must be a whole number.")
        _number_limits(value, rules)
        return value

    if type_ == "decimal":
        if isinstance(value, bool):
            raise ValidationError("Must be a number.")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValidationError("Must be a number.")
        if not number.is_finite():
            raise ValidationError("Must be a number.")
        _number_limits(number, rules)
        return str(number)  # a string keeps the exact digits through JSON

    if type_ == "boolean":
        if not isinstance(value, bool):
            raise ValidationError("Must be true or false.")
        return value

    if type_ == "date":
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError):
            raise ValidationError("Must be a date like 2026-01-31.")

    if type_ == "url":
        try:
            URLValidator(schemes=["http", "https"])(value)
        except (DjangoValidationError, TypeError):
            raise ValidationError("Must be a full http(s) URL.")
        return value

    if type_ == "color":
        if not isinstance(value, str) or not _COLOR.match(value):
            raise ValidationError("Must be a hex colour like #1a2b3c.")
        return value.lower()

    if type_ == "json":
        return value

    if type_ == "metaobject_reference":
        from .models import Metaobject

        try:
            entry_id = uuid.UUID(str(value))
        except ValueError:
            raise ValidationError("Must be the id of a metaobject.")
        entries = Metaobject.objects.filter(pk=entry_id, store=store)
        if rules.get("metaobject_type"):
            entries = entries.filter(definition__type=rules["metaobject_type"])
        if not entries.exists():
            raise ValidationError("That metaobject doesn't exist in this store.")
        return str(entry_id)

    raise ValidationError(f"Unknown type '{type_}'.")
