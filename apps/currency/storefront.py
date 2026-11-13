"""
The storefront side of multi-currency: which currency this visitor sees, how a base-currency price is shown in
it, the header selector, and the cookie that remembers the choice. Everything is off until the store turns on
`multi_currency_enabled`; until then none of this runs and pages render exactly as before.

The storefront is served in a sandbox (no script access to cookies), so the selector is a plain GET form
(?currency=USD) and the server sets the cookie.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from html import escape

from babel import Locale, UnknownLocaleError
from babel.numbers import get_territory_currencies

from . import money, services

PARAM = "currency"
COOKIE_AGE = 60 * 60 * 24 * 365
GEO_HEADERS = ("HTTP_CF_IPCOUNTRY", "HTTP_X_VERCEL_IP_COUNTRY", "HTTP_CLOUDFRONT_VIEWER_COUNTRY", "HTTP_X_COUNTRY_CODE")
SELECTOR_PLACEHOLDER = "<!--currency-selector-->"
# English is written differently per country (₹1,00,000 vs $100,000), so English visitors get their currency's own
ENGLISH_LOCALES = {"INR": "en_IN", "GBP": "en_GB", "AUD": "en_AU", "CAD": "en_CA", "NZD": "en_NZ", "SGD": "en_SG", "ZAR": "en_ZA"}

_active = ContextVar("fx_display", default=None)


@dataclass(frozen=True)
class Display:
    base: str
    code: str
    rate: Decimal  # display units per 1 base unit, markup not included
    markup: Decimal
    locale: str

    @property
    def converted(self):
        return self.code != self.base

    @property
    def effective_rate(self):
        return self.rate * (1 + self.markup / 100) if self.converted else self.rate

    def format_major(self, value):
        """A base-currency amount in major units (Decimal/str/int), converted and formatted."""
        minor = money.to_minor(Decimal(str(value)), self.base)
        return money.format_price(minor, self.base, self.code, self.locale, rate=self.rate, markup_percent=self.markup)


def current():
    return _active.get()


@contextmanager
def active(display):
    token = _active.set(display)
    try:
        yield display
    finally:
        _active.reset(token)


def cookie_name(store):
    return f"tss_currency_{store.store_slug}"


def _locale_for(code, accept_language):
    tag = (accept_language or "").split(",")[0].split(";")[0].strip().replace("-", "_")
    if tag:
        try:
            locale = Locale.parse(tag)
            if locale.language != "en":
                return str(locale)
            if locale.territory and locale.territory != "US":
                return str(locale)
        except (UnknownLocaleError, ValueError):
            pass
    return ENGLISH_LOCALES.get(code, "en_US")


def _geo_currency(request, allowed):
    for header in GEO_HEADERS:
        country = (request.META.get(header) or "").strip().upper()
        if len(country) == 2 and country.isalpha():
            try:
                codes = get_territory_currencies(country, tender=True)
            except (UnknownLocaleError, ValueError):
                continue
            for code in codes:
                if code in allowed:
                    return code
    return None


def build(store, code, accept_language=""):
    """The Display for `code` in this store, or None when multi-currency is off. Falls back to the base."""
    if not store.multi_currency_enabled:
        return None
    allowed = services.storefront_currencies(store)
    base = allowed[0]
    code = (code or base).upper()
    rate = services.get_rate(base, code) if code in allowed else None
    if rate is None:
        code, rate = base, Decimal(1)
    return Display(base, code, rate, Decimal(store.fx_markup_percent or 0), _locale_for(code, accept_language))


def resolve(store, request):
    """This visitor's Display: ?currency=, then their cookie, then their country's currency, then the base."""
    if not store.multi_currency_enabled or request is None:
        return None
    cached = getattr(request, "_fx_display", None)
    if cached is not None:
        return cached[0]
    allowed = services.storefront_currencies(store)
    asked = (request.GET.get(PARAM) or "").strip().upper()
    request._fx_chosen = asked if asked in allowed else None
    saved = (request.COOKIES.get(cookie_name(store)) or "").upper()
    code = request._fx_chosen or (saved if saved in allowed else None) or _geo_currency(request, allowed) or allowed[0]
    display = build(store, code, request.headers.get("Accept-Language", ""))
    request._fx_display = (display,)
    return display


@contextmanager
def for_request(store, request):
    display = resolve(store, request)
    if display is None:
        yield None
    else:
        with active(display) as d:
            yield d


def for_cart(store, cart, request=None):
    """The Display a cart's visitor chose (stored on the cart), for the API calls that have no cookie."""
    if cart is None or not store.multi_currency_enabled:
        return active(None)
    return active(build(store, cart.display_currency, request.headers.get("Accept-Language", "") if request else ""))


def remember(response, request, store):
    """Set the cookie when the visitor just picked a currency, and keep their open cart on it."""
    chosen = getattr(request, "_fx_chosen", None)
    display = resolve(store, request) if store.multi_currency_enabled else None
    if display is None:
        return
    from apps.themes.cart import clean_id, cookie_name as cart_cookie
    from apps.themes.rendering import shop_url

    if chosen:
        response.set_cookie(
            cookie_name(store), chosen, max_age=COOKIE_AGE, path=f"{shop_url(store)}/", samesite="Lax",
            secure=not _debug(),
        )
    cart_id = clean_id(request.COOKIES.get(cart_cookie(store), "")) or clean_id(getattr(request, "_cart_session", ("",))[0] or "")
    if cart_id:
        from apps.commerce.models import Cart

        Cart.objects.filter(store=store, session_id=cart_id).exclude(display_currency=display.code).update(display_currency=display.code)


def _debug():
    from django.conf import settings

    return settings.DEBUG


def selector_html(store, request, display, floating=True):
    options = "".join(
        f'<option value="{escape(c)}"{" selected" if c == display.code else ""}>{escape(c)} {escape(money.currency_info(c)["symbol"])}</option>'
        for c in services.storefront_currencies(store)
    )
    keep = "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">'
        for k in request.GET for v in request.GET.getlist(k) if k != PARAM
    )
    style = (
        "position:fixed;top:8px;right:8px;z-index:2147483000;background:#fff;border:1px solid #d4d4d8;"
        "border-radius:6px;padding:2px 4px;font:13px system-ui,sans-serif" if floating else "display:inline-block"
    )
    return (
        f'<form method="get" action="" data-currency-selector style="{style}">{keep}'
        f'<label style="position:absolute;left:-9999px">Currency</label>'
        f'<select name="{PARAM}" aria-label="Currency" onchange="this.form.submit()" '
        f'style="border:0;background:transparent;font:inherit;color:#18181b">{options}</select>'
        f"<noscript><button type=\"submit\">Go</button></noscript></form>"
    )


def _checkout_notice(html, store, display):
    """On the checkout page, say that converted prices are estimates and what the card is charged in."""
    if not display.converted or 'id="checkout-submit-btn"' not in html or "data-currency-notice" in html:
        return html
    _charge, notice = services.charge_currency(store, display.code)
    marker = html.find('id="checkout-submit-btn"')
    marker = html.rfind("<button", 0, marker)
    if not notice or marker == -1:
        return html
    box = f'<p data-currency-notice role="note" style="font:13px/1.4 system-ui,sans-serif;margin:8px 0">{escape(notice)}</p>'
    return html[:marker] + box + html[marker:]


def inject(html, store, request):
    """Add the currency selector to a rendered storefront page (at <!--currency-selector--> if the theme has it)."""
    display = resolve(store, request)
    if display is None or request.method != "GET" or request.headers.get("Sec-Fetch-Dest") == "iframe":
        return html
    html = _checkout_notice(html, store, display)
    if len(services.storefront_currencies(store)) < 2 or "data-currency-selector" in html:
        return html
    if SELECTOR_PLACEHOLDER in html:
        return html.replace(SELECTOR_PLACEHOLDER, selector_html(store, request, display, floating=False), 1)
    end = html.rfind("</body>")
    form = selector_html(store, request, display)
    return html[:end] + form + html[end:] if end != -1 else html + form
