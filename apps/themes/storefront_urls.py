"""
Public storefront pages, rendered from each store's theme, and the forms they post to.

The theme's JSON endpoints (cart, newsletter, wishlist, visit beacon) are versioned API routes:
/api/v1/storefront/<store_slug>/... (storefront_api_urls.py).
"""
from django.urls import path

from .sitemap_views import storefront_robots_txt_view, storefront_sitemap_view
from .privacy_views import storefront_cookie_consent, storefront_data_opt_out, storefront_marketing_confirm
from .views import (
    storefront_not_found, storefront_account, storefront_account_address, storefront_account_profile,
    storefront_article, storefront_blog, storefront_cart, storefront_checkout, storefront_checkout_order, storefront_collection, storefront_gift_card,
    storefront_gift_card_balance, storefront_home, storefront_login, storefront_logout, storefront_page,
    storefront_product, storefront_register, storefront_search, storefront_wishlist, theme_image,
)


urlpatterns = [
    path("_img/<uuid:image_id>", theme_image, name="theme-image"),
    path("<slug:store_slug>/account/login", storefront_login, name="storefront-login-noslash"),
    path("<slug:store_slug>/account/login/", storefront_login, name="storefront-login"),
    path("<slug:store_slug>/account/register", storefront_register, name="storefront-register-noslash"),
    path("<slug:store_slug>/account/register/", storefront_register, name="storefront-register"),
    path("<slug:store_slug>/account/logout", storefront_logout, name="storefront-logout-noslash"),
    path("<slug:store_slug>/account/logout/", storefront_logout, name="storefront-logout"),
    path("<slug:store_slug>/account/profile/", storefront_account_profile, name="storefront-account-profile"),
    path("<slug:store_slug>/account/addresses/", storefront_account_address, name="storefront-account-address"),
    path("<slug:store_slug>/account", storefront_account, name="storefront-account-noslash"),
    path("<slug:store_slug>/account/", storefront_account, name="storefront-account"),
    path("<slug:store_slug>/cart", storefront_cart, name="storefront-cart-page-noslash"),
    path("<slug:store_slug>/cart/", storefront_cart, name="storefront-cart-page"),
    path("<slug:store_slug>/checkout", storefront_checkout, name="storefront-checkout-page-noslash"),
    path("<slug:store_slug>/checkout/", storefront_checkout, name="storefront-checkout-page"),
    path("<slug:store_slug>/checkout/order/<uuid:order_id>/", storefront_checkout_order, name="storefront-checkout-order"),
    path("<slug:store_slug>/privacy/consent", storefront_cookie_consent, name="storefront-cookie-consent"),
    path("<slug:store_slug>/privacy/opt-out", storefront_data_opt_out, name="storefront-data-opt-out"),
    path("<slug:store_slug>/privacy/confirm/<str:token>", storefront_marketing_confirm, name="storefront-marketing-confirm"),
    path("<slug:store_slug>/gift-card", storefront_gift_card, name="storefront-gift-card"),
    path("<slug:store_slug>/gift-card/balance", storefront_gift_card_balance, name="storefront-gift-card-balance"),
    path("<slug:store_slug>/wishlist/", storefront_wishlist, name="storefront-wishlist"),
    path("<slug:store_slug>/", storefront_home, name="storefront-home"),
    path("<slug:store_slug>/products/<uuid:product_id>", storefront_product, name="storefront-product"),
    path("<slug:store_slug>/collections/all", storefront_collection, name="storefront-collection-all"),
    path("<slug:store_slug>/collections/<uuid:collection_id>", storefront_collection, name="storefront-collection"),
    path("<slug:store_slug>/collections/<slug:handle>", storefront_collection, name="storefront-collection-handle"),
    path("<slug:store_slug>/search", storefront_search, name="storefront-search-noslash"),
    path("<slug:store_slug>/search/", storefront_search, name="storefront-search"),
    path("<slug:store_slug>/pages/<slug:handle>", storefront_page, name="storefront-page"),
    path("<slug:store_slug>/blog", storefront_blog, name="storefront-blog"),
    path("<slug:store_slug>/blog/<slug:kind>/<slug:handle>", storefront_blog, name="storefront-blog-filter"),
    path("<slug:store_slug>/blog/<slug:handle>", storefront_article, name="storefront-article"),
    path("<slug:store_slug>/sitemap.xml", storefront_sitemap_view, name="storefront-sitemap"),
    path("<slug:store_slug>/robots.txt", storefront_robots_txt_view, name="storefront-robots"),
    # Anything else under a store: its redirect if it has one, else the not-found page. Keep this last.
    path("<slug:store_slug>/<path:rest>", storefront_not_found, name="storefront-not-found"),
]
