"""
URL configuration for config project.

The API is versioned in the path: /api/v1/... lives in config/api_v1.py.
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path("api/v1/", include("config.api_v1")),
    # Public storefronts, rendered from each store's theme
    path("s/", include("apps.themes.storefront_urls")),
]
