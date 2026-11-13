"""
Exchange rates and the money bookkeeping around them.

Rates are refreshed by a scheduled job, never by a page request: pages read the cache and, on a miss, the
database (the last-known-good rates). A failed refresh changes nothing.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import money
from .models import ExchangeRate

logger = logging.getLogger(__name__)

CACHE_KEY = "fx:rates:v1"
CACHE_SECONDS = 15 * 60


# -- Providers: {ISO code: units per 1 USD}. FX_PROVIDER = frankfurter (default, keyless, ~30 currencies) |
#    oxr (Open Exchange Rates, needs OXR_APP_ID) | static (FX_STATIC_RATES, tests/offline) ----------------

class ProviderError(Exception):
    pass


def _get_json(url, timeout=10):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "storefront-fx/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https endpoints)
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise ProviderError(f"{url.split('?')[0]}: {exc}") from exc


def _decimals(raw):
    out = {}
    for code, value in (raw or {}).items():
        try:
            rate = Decimal(str(value))
        except InvalidOperation:
            continue
        if rate > 0 and rate.is_finite():
            out[str(code).upper()] = rate
    return out


class RateProvider:
    name = ""

    def fetch(self):
        raise NotImplementedError


class FrankfurterProvider(RateProvider):
    name = "frankfurter"

    def fetch(self):
        base = getattr(settings, "FRANKFURTER_URL", "https://api.frankfurter.dev").rstrip("/")
        data = _get_json(f"{base}/v1/latest?base=USD")
        rates = _decimals(data.get("rates") if isinstance(data, dict) else None)
        if not rates:
            raise ProviderError("frankfurter: empty response")
        return rates


class OpenExchangeRatesProvider(RateProvider):
    name = "oxr"

    def fetch(self):
        app_id = getattr(settings, "OXR_APP_ID", "")
        if not app_id:
            raise ProviderError("oxr: OXR_APP_ID is not set")
        data = _get_json(f"https://openexchangerates.org/api/latest.json?{urllib.parse.urlencode({'app_id': app_id})}")
        rates = _decimals(data.get("rates") if isinstance(data, dict) else None)
        if not rates:
            raise ProviderError("oxr: empty response")
        return rates


class StaticProvider(RateProvider):
    name = "static"

    def fetch(self):
        rates = _decimals(getattr(settings, "FX_STATIC_RATES", {}))
        if not rates:
            raise ProviderError("static: FX_STATIC_RATES is empty")
        return rates


PROVIDERS = {"frankfurter": FrankfurterProvider, "oxr": OpenExchangeRatesProvider, "static": StaticProvider}


def get_provider(name=None):
    name = (name or getattr(settings, "FX_PROVIDER", "frankfurter") or "frankfurter").lower()
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ProviderError(f"Unknown FX_PROVIDER {name!r}") from None


# -- Rates ------------------------------------------------------------------------------------

def refresh_rates():
    """Fetch and store fresh rates. Returns True on success; on failure the stored rates stay as they were."""
    try:
        provider = get_provider()
        rates = provider.fetch()
        # Frankfurter covers ~30 currencies; a configured Open Exchange Rates key fills in the rest
        if isinstance(provider, FrankfurterProvider) and getattr(settings, "OXR_APP_ID", ""):
            try:
                for code, rate in OpenExchangeRatesProvider().fetch().items():
                    rates.setdefault(code, rate)
            except ProviderError as exc:
                logger.warning("FX supplement unavailable: %s", exc)
    except ProviderError as exc:
        logger.warning("FX refresh failed, keeping last-known-good rates: %s", exc)
        return False

    rates["USD"] = Decimal(1)
    now = timezone.now()
    rows = [
        ExchangeRate(code=code, per_usd=rate, provider=provider.name, fetched_at=now)
        for code, rate in rates.items()
        if money.is_valid(code)
    ]
    with transaction.atomic():
        ExchangeRate.objects.bulk_create(
            rows, update_conflicts=True, unique_fields=["code"], update_fields=["per_usd", "provider", "fetched_at"],
        )
    cache.delete(CACHE_KEY)
    return True


def load_rates():
    """{code: per_usd} for every currency with a usable (not too old) rate. Cache first, then the database."""
    rates = cache.get(CACHE_KEY)
    if rates is None:
        cutoff = timezone.now() - timedelta(hours=getattr(settings, "FX_RATE_MAX_AGE_HOURS", 168))
        rates = {r.code: r.per_usd for r in ExchangeRate.objects.filter(fetched_at__gte=cutoff)}
        cache.set(CACHE_KEY, rates, CACHE_SECONDS)
    return rates


def get_rate(from_code, to_code):
    """Units of to_code per one unit of from_code, or None when either has no usable rate."""
    from_code, to_code = from_code.upper(), to_code.upper()
    if from_code == to_code:
        return Decimal(1)
    rates = load_rates()
    if from_code not in rates or to_code not in rates:
        return None
    return rates[to_code] / rates[from_code]


def rate_available(code):
    return code.upper() in load_rates()


# -- Stores -----------------------------------------------------------------------------------

def base_currency(store):
    """The store's base currency. `currency` is the long-standing column; base_currency mirrors it."""
    return (store.currency or store.base_currency or "INR").strip().upper()


def storefront_currencies(store):
    """
    The currencies a visitor may pick: the base currency first, then the tenant's enabled ones that have a
    rate. Currencies with no rate are left out instead of being shown at a wrong price. Off by default.
    """
    base = base_currency(store)
    if not store.multi_currency_enabled:
        return [base]
    enabled = []
    for code in store.enabled_currencies or []:
        code = str(code).upper()
        if code != base and code not in enabled and money.is_valid(code) and rate_available(code):
            enabled.append(code)
    return [base, *enabled]


# -- ProductVariant.price_minor: integer minor units of the base currency, written with the decimals -----

def _as_stored(value):
    """The price as the legacy numeric(12,2) column holds it, so price_minor never disagrees with the decimals."""
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def minor_for(value, code):
    """Minor units of `value` in `code`; an unknown code falls back to two decimals instead of failing a save."""
    value = _as_stored(value)
    if value is None:
        return None
    try:
        return money.to_minor(value, code)
    except ValueError:
        return money.to_minor(value, "USD")


def fill_variant_minor(variant):
    from apps.tenancy.models import Store

    code = Store.objects.filter(pk=variant.product.store_id).values_list("currency", flat=True).first() or "INR"
    variant.price_minor = minor_for(variant.price, code)
    variant.compare_at_price_minor = minor_for(variant.compare_at_price, code)


def resync_store_prices(store):
    """Recompute every variant's minor units, e.g. after the store's base currency (and so its exponent) changed."""
    from apps.catalog.models import ProductVariant

    code = (store.currency or "INR").upper()
    if not money.is_valid(code):
        return 0
    changed = []
    for variant in ProductVariant.objects.filter(product__store=store).only(
        "id", "price", "compare_at_price", "price_minor", "compare_at_price_minor"
    ):
        price, compare = minor_for(variant.price, code), minor_for(variant.compare_at_price, code)
        if (price, compare) != (variant.price_minor, variant.compare_at_price_minor):
            variant.price_minor, variant.compare_at_price_minor = price, compare
            changed.append(variant)
    ProductVariant.objects.bulk_update(changed, ["price_minor", "compare_at_price_minor"], batch_size=500)
    return len(changed)


# -- Checkout and orders. Orders are priced, taxed and charged in the base currency; the currency the shopper
#    browsed in is an estimate, recorded with the rate they were shown. Charging in another currency is not
#    offered yet: refunds, disputes and "paid in full" checks assume the payment is in the order's currency.

CHARGE_NOTICE = (
    "Prices in {display} are estimates at today's exchange rate. Your order is charged in {base}, "
    "so the amount on your card statement may differ slightly."
)


def charge_currency(store, selected=None):
    """-> (currency the payment gateway is charged in, notice shown to the shopper or '')."""
    base = base_currency(store)
    selected = (selected or "").upper()
    if selected and selected != base:
        return base, CHARGE_NOTICE.format(display=selected, base=base)
    return base, ""


def snapshot_checkout(checkout):
    """Record on the checkout which currency the shopper browsed in and the rate they were shown."""
    store = checkout.store
    display = (getattr(checkout.cart, "display_currency", "") or "").upper()
    rate = None
    if store.multi_currency_enabled and display and display != base_currency(store) and display in storefront_currencies(store):
        rate = get_rate(base_currency(store), display)
    if rate is None:
        checkout.display_currency, checkout.fx_rate = "", None
        return
    checkout.display_currency = display
    checkout.fx_rate = rate * (1 + Decimal(store.fx_markup_percent or 0) / 100)


def record_order(order, checkout):
    """Copy the snapshot to the order: display currency, the rate used, and the total in base minor units."""
    base = (order.currency_code or "").upper()
    fields = {
        "base_currency": base,
        "base_amount_minor": money.to_minor(order.total, base),
        "display_currency": checkout.display_currency or "",
        "fx_rate": checkout.fx_rate,
    }
    for name, value in fields.items():
        setattr(order, name, value)
    type(order).objects.filter(pk=order.pk).update(**fields)
