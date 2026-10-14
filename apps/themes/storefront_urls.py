"""Public storefront pages, rendered from each store's theme."""
from django.urls import path

from .views import (
    storefront_article, storefront_blog, storefront_collection, storefront_home, storefront_product, theme_image,
)


urlpatterns = [
    path("_img/<uuid:image_id>", theme_image, name="theme-image"),
    path("<slug:store_slug>/", storefront_home, name="storefront-home"),
    path("<slug:store_slug>/products/<uuid:product_id>", storefront_product, name="storefront-product"),
    path("<slug:store_slug>/collections/all", storefront_collection, name="storefront-collection-all"),
    path("<slug:store_slug>/collections/<uuid:collection_id>", storefront_collection, name="storefront-collection"),
    path("<slug:store_slug>/collections/<slug:handle>", storefront_collection, name="storefront-collection-handle"),
    path("<slug:store_slug>/blog", storefront_blog, name="storefront-blog"),
    path("<slug:store_slug>/blog/<slug:kind>/<slug:handle>", storefront_blog, name="storefront-blog-filter"),
    path("<slug:store_slug>/blog/<slug:handle>", storefront_article, name="storefront-article"),
]
