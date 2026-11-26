"""
URL configuration for config project.

Every API route is versioned in the path: /api/v1/... lives in config/api_v1.py (health checks included).
Outside it are only the Django admin and the storefront's HTML pages (/s/<store_slug>/...).
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path("api/v1/", include("config.api_v1")),
    # Public storefronts, rendered from each store's theme
    path("s/", include("apps.themes.storefront_urls")),
]

# Uploaded product photos, for local development only
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = "apps.core.errors.not_found"
handler500 = "apps.core.errors.server_error"
