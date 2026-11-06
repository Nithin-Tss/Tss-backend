"""
ISO 4217 currency support for gift cards, backed by Babel (already a dependency) instead of a
hand-maintained table. Each currency has its own number of decimal places (JPY 0, USD 2, KWD 3);
money for a currency is always quantized to that currency's own places, never assumed to be 2.
"""
from decimal import Decimal

from babel.numbers import format_currency, get_currency_precision, list_currencies

DEFAULT_CURRENCY = "AUD"
DEFAULT_AMOUNTS = [Decimal("25"), Decimal("50"), Decimal("100"), Decimal("200")]

_VALID_CODES = {code.upper() for code in list_currencies()}


def is_valid(code):
    return bool(code) and code.upper() in _VALID_CODES


def decimal_places(code):
    return get_currency_precision(code.upper())


def quantize(code, amount):
    places = decimal_places(code)
    return Decimal(str(amount)).quantize(Decimal(1).scaleb(-places))


def format_amount(code, amount):
    return format_currency(quantize(code, amount), code.upper(), locale="en_US")


def validate_code(code):
    from rest_framework.exceptions import ValidationError

    code = (code or "").strip().upper()
    if not is_valid(code):
        raise ValidationError({"currency_code": "Enter a valid ISO 4217 currency code."})
    return code
