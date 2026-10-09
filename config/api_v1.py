"""
Version 1 of the API, mounted at /api/v1/ (see config/urls.py).
A breaking change gets a new module (api_v2.py) and a new mount point;
v1 keeps working unchanged.
"""
from django.urls import include, path

urlpatterns = [
    path("auth/", include("apps.identity.urls")),
    path("stores/", include("apps.tenancy.urls")),
    path("catalog/", include("apps.catalog.urls")),
    path("customers/", include("apps.customers.urls")),
    path("orders/", include("apps.orders.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("discounts/", include("apps.discounts.urls")),
    path("themes/", include("apps.themes.urls")),
    # Commerce: /api/v1/carts/, /api/v1/checkouts/ (staff, read-only)
    path("", include("apps.commerce.urls")),
    # Shoppers: /api/v1/storefront/<store_slug>/cart/ ...
    path("storefront/", include("apps.commerce.storefront_urls")),
]
