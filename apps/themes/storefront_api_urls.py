"""
The storefront theme's JSON endpoints, mounted at /api/v1/storefront/ (see config/api_v1.py).

The theme's scripts call these (cart drawer, newsletter form, wishlist hearts, visit beacon) using
{{ shop.api_url }}. They identify the shopper by the X-Cart-Session header (or the form's cart field),
never by cookie: the storefront page runs sandboxed, so no cookie reaches them anyway.

Pages, and the forms that sign shoppers in or place orders (they rely on cookies scoped to the store's
/s/<store_slug>/ pages), stay in storefront_urls.py.

Each route answers with and without the trailing slash: the cart script appends "/add", "/change", ...
to its base, and POSTs cannot follow an APPEND_SLASH redirect.
"""
from django.urls import path

from .views import (
    storefront_cart_add, storefront_cart_change, storefront_cart_clear, storefront_cart_fragment,
    storefront_newsletter, storefront_track, storefront_wishlist_item,
)

_ROUTES = (
    ("cart/fragment", storefront_cart_fragment, "storefront-cart-fragment"),
    ("cart/add", storefront_cart_add, "storefront-cart-add"),
    ("cart/change", storefront_cart_change, "storefront-cart-change"),
    ("cart/clear", storefront_cart_clear, "storefront-cart-clear"),
    ("newsletter", storefront_newsletter, "storefront-newsletter"),
    ("track", storefront_track, "storefront-track"),
    ("wishlist/items", storefront_wishlist_item, "storefront-wishlist-item"),
)

urlpatterns = [
    route
    for suffix, view, name in _ROUTES
    for route in (
        path(f"<slug:store_slug>/{suffix}/", view, name=name),
        path(f"<slug:store_slug>/{suffix}", view, name=f"{name}-noslash"),
    )
]
