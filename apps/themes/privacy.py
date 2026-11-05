"""Storefront enforcement of Settings > Customer privacy.

Injected into every rendered storefront page (so it works whatever the store's theme code says):
  * the cookie banner (cookie_banner: automated | always | off),
  * a "Your privacy choices" link to the data sharing opt out page (data_sharing_opt_out),
  * the e-mail / SMS marketing checkboxes on the checkout page (checkout_*_marketing),
and the helpers that record marketing consent, with double opt-in confirmation by e-mail.

"automated" means: shown to visitors from regions whose law asks for it (judged from the CDN's country header),
and to everyone when the region is unknown.
"""
import logging
from html import escape
from urllib.parse import quote, urlsplit

from django.core import signing
from django.utils import timezone

from .rendering import shop_url

logger = logging.getLogger(__name__)

CONSENT_COOKIE_AGE = 60 * 60 * 24 * 365
CONFIRM_MAX_AGE = 60 * 60 * 24 * 7
CONFIRM_SALT = "storefront-marketing-confirm"

COUNTRY_HEADERS = ("CF-IPCountry", "X-Vercel-IP-Country", "CloudFront-Viewer-Country", "X-Country-Code")
# Regions whose law asks for cookie consent: EU / EEA, UK, Switzerland, Brazil, Canada, South Korea
COOKIE_CONSENT_REGIONS = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
    "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "CH", "BR", "CA", "KR",
}
# Regions with a "do not sell or share my data" right (US state laws)
OPT_OUT_REGIONS = {"US"}


def visitor_country(request):
    for header in COUNTRY_HEADERS:
        value = (request.headers.get(header) or "").strip().upper()
        if len(value) == 2 and value.isalpha() and value not in ("XX", "T1"):
            return value
    return ""


def _shown(mode, request, regions):
    if mode == "always":
        return True
    if mode == "off":
        return False
    country = visitor_country(request)
    return not country or country in regions  # automated; unknown region -> show


def cookie_banner_shown(store, request):
    return _shown(store.cookie_banner, request, COOKIE_CONSENT_REGIONS)


def opt_out_offered(store, request):
    return _shown(store.data_sharing_opt_out, request, OPT_OUT_REGIONS)


def consent_cookie(store):
    return f"tss_cc_{store.store_slug}"


def opt_out_cookie(store):
    return f"tss_dso_{store.store_slug}"


def safe_next(store, value):
    """Where to go back to after a banner choice: only inside this store."""
    base = shop_url(store)
    parts = urlsplit(value or "")
    if parts.scheme or parts.netloc or not (parts.path == base or parts.path.startswith(base + "/")):
        return base + "/"
    return parts.path + (f"?{parts.query}" if parts.query else "")


# ------------------------------------------------------------------ page injection

_STYLE = (
    "font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#14130F;"
)


def _banner_html(store, request, base):
    here = request.get_full_path()
    q = "next=" + quote(here, safe="")
    return (
        '<div id="tss-cookie-banner" role="dialog" aria-label="Cookie consent" style="position:fixed;left:16px;right:16px;'
        "bottom:16px;max-width:560px;margin:0 auto;z-index:2147483000;background:#fff;border:1px solid #d8d6cf;"
        f'border-radius:14px;padding:16px;box-shadow:0 8px 30px rgba(0,0,0,.18);{_STYLE}">'
        f'<p style="margin:0 0 12px">{escape(store.store_name)} uses cookies to run the store and, with your '
        "permission, to understand how it is used.</p>"
        '<div style="display:flex;gap:8px;flex-wrap:wrap">'
        f'<a href="{base}/privacy/consent?choice=accept&amp;{q}" style="background:#14130F;color:#fff;text-decoration:none;'
        'padding:8px 16px;border-radius:8px;font-weight:600">Accept</a>'
        f'<a href="{base}/privacy/consent?choice=decline&amp;{q}" style="border:1px solid #14130F;color:#14130F;'
        'text-decoration:none;padding:8px 16px;border-radius:8px;font-weight:600">Decline</a>'
        "</div></div>"
    )


def _opt_out_link(base):
    return (
        f'<a id="tss-privacy-choices" href="{base}/privacy/opt-out" style="position:fixed;left:16px;bottom:4px;'
        f'z-index:2147482000;font-size:12px;color:#555;background:rgba(255,255,255,.85);padding:2px 6px;'
        f'border-radius:6px;{_STYLE.replace("14px/1.4", "12px/1.4")}">Your privacy choices</a>'
    )


def _consent_html(store, base):
    def box(name, label, mode):
        if mode == "hidden":
            return ""
        checked = " checked" if mode == "prechecked" else ""
        return (
            f'<label style="display:flex;gap:10px;align-items:flex-start;margin:6px 0;cursor:pointer">'
            f'<input type="checkbox" name="{name}" data-tss-consent="{name}"{checked} style="margin-top:3px">'
            f"<span>{label}</span></label>"
        )

    inner = box("email_marketing", f"Email me news and offers from {escape(store.store_name)}", store.checkout_email_marketing)
    inner += box("sms_marketing", f"Text me news and offers from {escape(store.store_name)}", store.checkout_sms_marketing)
    if not inner:
        return ""
    # The theme's own script builds the order request, so tag it with the consent choices
    script = (
        "<script>(function(){var f=window.fetch;if(!f)return;window.fetch=function(u,o){try{"
        "if(o&&typeof o.body==='string'&&o.body.indexOf('\"place_order\"')>-1){var b=JSON.parse(o.body);"
        "document.querySelectorAll('[data-tss-consent]').forEach(function(c){b[c.getAttribute('data-tss-consent')]=c.checked;});"
        "o=Object.assign({},o,{body:JSON.stringify(b)});}}catch(e){}return f.call(this,u,o);};})();</script>"
    )
    return (
        f'<div class="checkout-card" id="tss-marketing-consent" style="{_STYLE}"><h2 class="checkout-card__title">'
        f"Marketing preferences</h2>{inner}</div>{script}"
    )


def inject(html, store, request):
    """Add the privacy features to a rendered storefront page."""
    if not isinstance(html, str) or request is None or request.method != "GET":
        return html
    if request.headers.get("Sec-Fetch-Dest") == "iframe":  # the customizer's live preview
        return html
    base = shop_url(store)

    if 'id="checkout-submit-btn"' in html and 'id="tss-marketing-consent"' not in html:
        card = _consent_html(store, base)
        if card:
            marker = html.find('<button type="submit" id="checkout-submit-btn"')
            if marker == -1:
                marker = html.find('id="checkout-submit-btn"')
                marker = html.rfind("<button", 0, marker) if marker != -1 else -1
            if marker != -1:
                html = html[:marker] + card + html[marker:]

    extra = ""
    if cookie_banner_shown(store, request) and not request.COOKIES.get(consent_cookie(store)):
        extra += _banner_html(store, request, base)
    if opt_out_offered(store, request):
        extra += _opt_out_link(base)
    if extra:
        end = html.rfind("</body>")
        html = html[:end] + extra + html[end:] if end != -1 else html + extra
    return html


# ------------------------------------------------------------------ marketing consent

def _token(customer, channel):
    return signing.dumps({"c": str(customer.pk), "ch": channel}, salt=CONFIRM_SALT)


def read_token(token):
    try:
        data = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
    except signing.BadSignature:
        return None
    return data if data.get("ch") in ("email", "sms") else None


def _send_confirmation(store, customer, request, channel):
    from apps.notifications import services as notify

    link = request.build_absolute_uri(f"{shop_url(store)}/privacy/confirm/{_token(customer, channel)}")
    what = "marketing emails" if channel == "email" else "marketing text messages"
    config = notify.get_settings(store)
    return notify.send(store, config, f"Confirm your subscription to {store.store_name}", [
        f"Please confirm that you want to receive {what} from {store.store_name}.",
        f"Confirm: {link}",
        "If you didn't ask for this, ignore this email and nothing will change.",
    ], [customer.email])


def record_consent(store, customer, request, email=False, sms=False):
    """Record what a shopper agreed to. With double opt-in the subscription starts once they confirm by email."""
    if customer is None or not customer.email:
        return
    meta = dict(customer.metadata or {})
    fields = []
    for channel, wanted, double in (("email", email, store.double_opt_in_email), ("sms", sms, store.double_opt_in_sms)):
        if not wanted:
            continue
        if double:
            meta[f"{channel}_marketing"] = "pending"
            meta[f"{channel}_marketing_requested_at"] = timezone.now().isoformat()
            _send_confirmation(store, customer, request, channel)
        else:
            meta[f"{channel}_marketing"] = "subscribed"
            if channel == "email":
                customer.email_subscription_status = "subscribed"
                fields.append("email_subscription_status")
    customer.metadata = meta
    customer.save(update_fields=["metadata", "updated_at", *fields])


def confirm(store, token):
    """Finish a double opt-in from the emailed link. True when it worked."""
    from apps.customers.models import Customer

    data = read_token(token)
    customer = Customer.objects.filter(store=store, pk=data["c"]).first() if data else None
    if customer is None:
        return False
    meta = dict(customer.metadata or {})
    meta[f"{data['ch']}_marketing"] = "subscribed"
    customer.metadata = meta
    fields = ["metadata", "updated_at"]
    if data["ch"] == "email":
        customer.email_subscription_status = "subscribed"
        fields.append("email_subscription_status")
    customer.save(update_fields=fields)
    return True


def record_opt_out(store, email):
    """Remember a shopper's data sharing opt out against their customer record, when we know who they are."""
    from apps.customers.models import Customer

    customer = Customer.objects.filter(store=store, email__iexact=email).first() if email else None
    if customer is None:
        return False
    meta = dict(customer.metadata or {})
    meta["data_sharing_opt_out"] = timezone.now().isoformat()
    customer.metadata = meta
    customer.save(update_fields=["metadata", "updated_at"])
    return True


# ------------------------------------------------------------------ standalone pages

def page(store, title, body_html):
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><title>{escape(title)} · {escape(store.store_name)}</title></head>'
        f'<body style="margin:0;background:#faf9f6;{_STYLE}"><main style="max-width:560px;margin:64px auto;padding:0 20px">'
        f'<h1 style="font-size:24px">{escape(title)}</h1>{body_html}'
        f'<p style="margin-top:32px"><a href="{shop_url(store)}/" style="color:#14130F">← Back to {escape(store.store_name)}</a></p>'
        "</main></body></html>"
    )
