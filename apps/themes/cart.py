"""
The storefront cart: what the cart snippet (snippets/cart.liquid) shows, worked out on the server.

A shopper has no account: their cart is found by an id the server put in their page. It is kept in a cookie
for page loads, and sent back by the page's script in the X-Cart-Session header (the storefront runs in a
sandbox, so its scripts can neither read cookies nor send them). The id is random, so only the shopper who
was given it can reach the cart. Prices come from the lines (frozen by apps.commerce when they were added),
stock from apps.inventory: nothing a browser sends is trusted.
"""
import hashlib
import hmac
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings as django_settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db.models import Sum
from django.utils import timezone

from apps.commerce import services
from apps.commerce.models import Cart, CartItem
from apps.inventory.services import sellable

from . import section_sync as sync

CART_COOKIE_PREFIX = "tss_cart_"
CART_COOKIE_AGE = 30 * 24 * 60 * 60
CART_ID = re.compile(r"^[0-9a-f]{32}$")

# The "Cart icon" header block's options and what they default to (the block's schema in the header)
CART_DEFAULTS = {
    "label": "Cart",
    "show_count": True,
    "opens": "drawer",
    "size": 24,
    "drawer_title": "Cart",
    "checkout_label": "Check out",
    "checkout_url": "",
    "empty_message": "Your cart is empty",
    "show_discount": True,
    "show_login": False,
    "login_url": "/account",
}


def cookie_name(store):
    return f"{CART_COOKIE_PREFIX}{store.store_slug}"


def clean_id(value):
    """The id if it looks like one this server made, otherwise ''."""
    return value if isinstance(value, str) and CART_ID.match(value) else ""


def new_id():
    return uuid.uuid4().hex


# ---- the wishlist
# The storefront runs in a sandbox: its clicks cannot send the browser's session cookie. So the wishlist is kept
# under the shopper's cart id too (the page puts it in the heart's form), in the session table under its own key.
# A browser that sends its session cookie but no id (an older page) keeps using the session.

def _wishlist_row_key(cart_id):
    return f"wl{cart_id}"


def get_wishlist(store, request, cart_id):
    """The product ids this shopper saved in this store: the list kept under their cart id, else the session's."""
    cart_id = clean_id(cart_id)
    if cart_id:
        row = Session.objects.filter(session_key=_wishlist_row_key(cart_id), expire_date__gt=timezone.now()).first()
        if row is not None:
            ids = SessionStore().decode(row.session_data).get(f"wishlist:{store.pk}")
            if isinstance(ids, list):
                return ids
    ids = request.session.get(f"wishlist:{store.pk}", []) if request is not None else []
    return ids if isinstance(ids, list) else []


def put_wishlist(store, request, cart_id, ids):
    """Keep the shopper's list: under their cart id when they have one, otherwise in the session."""
    cart_id = clean_id(cart_id)
    if not cart_id:
        if request is not None:
            request.session[f"wishlist:{store.pk}"] = list(ids)
        return
    codec = SessionStore()
    key = _wishlist_row_key(cart_id)
    row = Session.objects.filter(session_key=key).first()
    data = codec.decode(row.session_data) if row is not None else {}
    data[f"wishlist:{store.pk}"] = list(ids)
    Session.objects.update_or_create(session_key=key, defaults={
        "session_data": codec.encode(data),
        "expire_date": timezone.now() + timedelta(seconds=CART_COOKIE_AGE),
    })


def preview_id(store, user):
    """
    The cart id of the customizer's preview: one test cart per signed-in owner and store, so Add to cart
    works in the preview like on the store. Not guessable (keyed with the server's secret), and it is
    never a shopper's cart.
    """
    seed = f"cart-preview:{store.pk}:{getattr(user, 'pk', '')}".encode()
    return hmac.new(django_settings.SECRET_KEY.encode(), seed, hashlib.sha256).hexdigest()[:32]


def session_for_page(store, request):
    """(cart id, True when the browser needs the cookie) for a page request: the cookie's id, the link's, or a new one."""
    if request is None:
        return "", False
    existing = clean_id(request.COOKIES.get(cookie_name(store), ""))
    if existing:
        return existing, False
    # No cookie (the browser did not keep it): the cart link carries the id the page's script was given
    from_link = clean_id(request.GET.get("cart", ""))
    return (from_link, True) if from_link else (new_id(), True)


def open_cart(store, session_id):
    """This shopper's open cart in this store, or None. Never creates one."""
    if not session_id:
        return None
    return Cart.objects.filter(store=store, session_id=session_id, status=services.CART_OPEN, is_deleted=False).first()


def item_count(store, session_id):
    if not session_id:
        return 0
    total = CartItem.objects.filter(
        cart__store=store, cart__session_id=session_id, cart__status=services.CART_OPEN,
        cart__is_deleted=False, is_deleted=False,
    ).aggregate(total=Sum("quantity"))["total"]
    return total or 0


def _line(item, base_url):
    variant = item.variant
    selling_on = bool(variant and variant.continue_selling)
    available = sellable(variant) if variant is not None else 0
    warning = ""
    if not selling_on:
        if available <= 0:
            warning = "Sold out"
        elif item.quantity > available:
            warning = f"Only {available} left"
    images = sorted(item.product.images.all(), key=lambda i: i.position)
    return {
        "id": str(item.pk),
        "title": item.product.title,
        "url": f"{base_url}/products/{item.product_id}",
        "image": images[0].url if images else "",
        "unit_price": str(item.unit_price),
        "quantity": item.quantity,
        "total": str(item.unit_price * item.quantity),
        "at_max": (not selling_on) and item.quantity >= available,
        "warning": warning,
    }


def view_data(cart, base_url):
    """{count, subtotal, lines}: everything the snippet shows, all worked out here."""
    if cart is None:
        return {"count": 0, "subtotal": "0.00", "lines": []}
    items = list(
        cart.items.filter(is_deleted=False)
        .select_related("product", "variant")
        .prefetch_related("product__images")
        .order_by("created_at")
    )
    subtotal = sum((i.unit_price * i.quantity for i in items), Decimal("0.00"))
    return {
        "count": sum(i.quantity for i in items),
        "subtotal": str(subtotal),
        "lines": [_line(i, base_url) for i in items],
    }


def _safe_url(value):
    value = str(value or "").strip()
    return value if value and sync._SAFE_URL.match(value) else ""


def options(store_theme, files=None):
    """The "Cart icon" header block's settings (the first visible one), or the defaults when the header has none."""
    from .services import synced_section

    chosen = {}
    header = synced_section(store_theme, "header", files)
    if header:
        block = next((b for b in header["values"]["blocks"] if b["type"] == "cart" and not b.get("hidden")), None)
        chosen = dict(block["settings"]) if block else {}
    merged = {**CART_DEFAULTS, **chosen}
    merged["checkout_url"] = _safe_url(merged["checkout_url"])
    merged["login_url"] = _safe_url(merged["login_url"]) or CART_DEFAULTS["login_url"]
    return merged
