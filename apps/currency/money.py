"""
ISO 4217 data (from Babel), integer-safe money maths and price formatting.

Amounts are integers in the currency's minor unit (paise, cents, fils; JPY has none). Rates and markups are
Decimal, never float, and rounding is half-up at the currency's own exponent.
"""
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import lru_cache

from babel import Locale, UnknownLocaleError
from babel.numbers import format_currency, get_currency_name, get_currency_precision, get_currency_symbol, list_currencies

HUNDRED = Decimal(100)
_CODES = frozenset(code.upper() for code in list_currencies())


# -- ISO 4217 ---------------------------------------------------------------------------------

def is_valid(code):
    return isinstance(code, str) and code.upper() in _CODES


def exponent(code):
    """Digits after the decimal point for this currency's minor unit (JPY 0, USD 2, KWD 3)."""
    code = (code or "").upper()
    if code not in _CODES:
        raise ValueError(f"Unknown ISO 4217 currency: {code!r}")
    return get_currency_precision(code)


@lru_cache(maxsize=None)
def currency_info(code, locale="en"):
    code = code.upper()
    return {
        "code": code,
        "name": get_currency_name(code, locale=Locale.parse(locale)),
        "symbol": get_currency_symbol(code, locale=Locale.parse(locale)),
        "exponent": exponent(code),
    }


def all_currencies(locale="en"):
    """Every ISO 4217 currency, A to Z by code."""
    return [currency_info(code, locale) for code in sorted(_CODES)]


# -- Maths ------------------------------------------------------------------------------------

def to_decimal(value):
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError("Floats are not accepted for money; pass Decimal, int or str.")
    return Decimal(str(value))


def to_minor(amount, code):
    """A major-unit amount ("399.00") as integer minor units (39900), rounded half-up."""
    exp = exponent(code)
    with localcontext() as ctx:
        ctx.prec = 40
        return int((to_decimal(amount) * (Decimal(10) ** exp)).to_integral_value(ROUND_HALF_UP))


def from_minor(minor, code):
    """Integer minor units as a Decimal in major units, with exactly the currency's decimal places."""
    exp = exponent(code)
    with localcontext() as ctx:
        ctx.prec = 40
        return (Decimal(int(minor)) / (Decimal(10) ** exp)).quantize(Decimal(1).scaleb(-exp))


def convert_minor(minor, from_code, to_code, rate, markup_percent=0):
    """
    Convert integer minor units between currencies: `rate` is units of to_code per one unit of from_code (major
    units). The optional markup is a percentage added on top. Rounded half-up once, at the end.
    """
    if from_code.upper() == to_code.upper() and not markup_percent:
        return int(minor)
    shift = exponent(to_code) - exponent(from_code)
    with localcontext() as ctx:
        ctx.prec = 40
        value = Decimal(int(minor)) * to_decimal(rate) * (Decimal(10) ** shift)
        value = value * (1 + to_decimal(markup_percent) / HUNDRED)
        return int(value.to_integral_value(ROUND_HALF_UP))


# -- Formatting -------------------------------------------------------------------------------

def _locale(value):
    try:
        return Locale.parse(str(value or "en").replace("-", "_"))
    except (UnknownLocaleError, ValueError):
        return Locale.parse("en")


def convert_price(amount_minor, base_currency, display_currency, *, rate=None, markup_percent=0):
    """
    -> (currency, minor units) actually shown. If the display currency has no rate, the base currency is shown
    unchanged: a missing rate must never produce a wrong price.
    """
    from . import services

    base_currency, display_currency = base_currency.upper(), (display_currency or base_currency).upper()
    if display_currency == base_currency:
        return base_currency, int(amount_minor)
    rate = rate if rate is not None else services.get_rate(base_currency, display_currency)
    if rate is None:
        return base_currency, int(amount_minor)
    return display_currency, convert_minor(amount_minor, base_currency, display_currency, rate, markup_percent)


def format_price(amount_minor, base_currency, display_currency=None, locale="en", *, rate=None, markup_percent=0):
    """formatPrice: convert with the cached rate, then format with the locale's symbol, decimals and grouping."""
    if amount_minor is None:
        return ""
    code, minor = convert_price(amount_minor, base_currency, display_currency, rate=rate, markup_percent=markup_percent)
    return format_currency(from_minor(minor, code), code, locale=_locale(locale), currency_digits=True)
