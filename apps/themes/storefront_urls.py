"""Public storefront pages, rendered from each store's theme."""
from django.urls import path

from .views import (
    storefront_article, storefront_blog, storefront_cart, storefront_cart_add, storefront_cart_change, storefront_cart_clear,
    storefront_cart_fragment, storefront_collection, storefront_home, storefront_page,
    storefront_product,
    storefront_wishlist, storefront_wishlist_item, theme_image,
)


urlpatterns = [
    path("_img/<uuid:image_id>", theme_image, name="theme-image"),
    path("<slug:store_slug>/cart", storefront_cart, name="storefront-cart-page-noslash"),
    path("<slug:store_slug>/cart/", storefront_cart, name="storefront-cart-page"),
    path("<slug:store_slug>/cart/fragment", storefront_cart_fragment, name="storefront-cart-fragment"),
    path("<slug:store_slug>/cart/add", storefront_cart_add, name="storefront-cart-add"),
    path("<slug:store_slug>/cart/change", storefront_cart_change, name="storefront-cart-change"),
    path("<slug:store_slug>/cart/clear", storefront_cart_clear, name="storefront-cart-clear"),
    path("<slug:store_slug>/wishlist/", storefront_wishlist, name="storefront-wishlist"),
    path("<slug:store_slug>/wishlist/items/", storefront_wishlist_item, name="storefront-wishlist-item"),
    path("<slug:store_slug>/", storefront_home, name="storefront-home"),
    path("<slug:store_slug>/products/<uuid:product_id>", storefront_product, name="storefront-product"),
    path("<slug:store_slug>/collections/all", storefront_collection, name="storefront-collection-all"),
    path("<slug:store_slug>/collections/<uuid:collection_id>", storefront_collection, name="storefront-collection"),
    path("<slug:store_slug>/collections/<slug:handle>", storefront_collection, name="storefront-collection-handle"),
    path("<slug:store_slug>/pages/<slug:handle>", storefront_page, name="storefront-page"),
    path("<slug:store_slug>/blog", storefront_blog, name="storefront-blog"),
    path("<slug:store_slug>/blog/<slug:kind>/<slug:handle>", storefront_blog, name="storefront-blog-filter"),
    path("<slug:store_slug>/blog/<slug:handle>", storefront_article, name="storefront-article"),
]
