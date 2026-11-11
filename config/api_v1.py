"""
Version 1 of the API, mounted at /api/v1/ (see config/urls.py).
A breaking change gets a new module (api_v2.py) and a new mount point;
v1 keeps working unchanged.
"""
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from apps.core.health import JobsHealthView
from apps.core.report_views import ReportDetailView, ReportListView
from apps.core.reports import AnalyticsView, DailyMetricsView, LiveView, OverviewView
from apps.core.support import SupportContactView
from apps.tenancy.team_urls import members_urlpatterns, roles_urlpatterns

urlpatterns = [
    # API documentation (OpenAPI 3)
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema", template_name="docs/swagger.html"), name="swagger-ui"),
    path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("health/jobs/", JobsHealthView.as_view(), name="health-jobs"),
    # Help Center "Contact support": emails SUPPORT_EMAIL, stores nothing
    path("support/contact/", SupportContactView.as_view(), name="support-contact"),
    path("auth/", include("apps.identity.urls")),
    path("stores/", include("apps.tenancy.urls")),
    path("members/", include(members_urlpatterns)),
    path("roles/", include(roles_urlpatterns)),
    path("audit/", include("apps.audit.urls")),
    # AI assistant: answers 404 unless config/assistant_local_settings.py is the settings module
    path("assistant/", include("apps.assistant.urls")),
    # AI page builder (drafts only): off unless the ai_page_builder feature flag is on for the store
    path("ai-assistant/", include("apps.ai_assistant.urls")),
    path("", include("apps.platform_admin.urls")),
    # Help Center tickets: /support/tickets/ (owners) and /platform/support/ (staff)
    path("", include("apps.helpdesk.urls")),
    path("catalog/", include("apps.catalog.urls")),
    path("metafields/", include("apps.metafields.urls")),
    path("customers/", include("apps.customers.urls")),
    path("orders/", include("apps.orders.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("discounts/", include("apps.discounts.urls")),
    path("themes/", include("apps.themes.urls")),
    path("media/", include("apps.media.urls")),
    path("payments/", include("apps.payments.urls")),
    path("gift-cards/", include("apps.gift_cards.urls")),
    path("fulfillments/", include("apps.fulfillment.urls")),
    path("delivery/", include("apps.delivery.urls")),
    path("wishlists/", include("apps.wishlist.urls")),
    path("webhooks/", include("apps.webhooks.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("reports/overview/", OverviewView.as_view(), name="reports-overview"),
    path("reports/analytics/", AnalyticsView.as_view(), name="reports-analytics"),
    path("reports/live/", LiveView.as_view(), name="reports-live"),
    path("reports/daily-metrics/", DailyMetricsView.as_view(), name="reports-daily-metrics"),
    path("reports/<slug:report_id>/", ReportDetailView.as_view(), name="reports-detail"),
    path("reports/", ReportListView.as_view(), name="reports"),
    path("purchase-orders/", include("apps.purchasing.urls")),
    # Commerce: /api/v1/carts/, /api/v1/checkouts/ (staff, read-only)
    path("", include("apps.commerce.urls")),
    # Shoppers: /api/v1/storefront/<store_slug>/cart/ ...
    path("storefront/", include("apps.commerce.storefront_urls")),
]
