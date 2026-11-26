"""Every API route is versioned: it lives under /api/v1/. Only the Django admin and the storefront's pages are outside."""
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver

# Not APIs: the Django admin, the storefront's HTML pages (/s/<store_slug>/...), and uploaded media in development
UNVERSIONED_PREFIXES = ("api/v1/", "admin/", "s/", "media/", "^media/")


def _routes(patterns, prefix=""):
    for entry in patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _routes(entry.url_patterns, route)
        elif isinstance(entry, URLPattern):
            yield route


class ApiVersioningTest(SimpleTestCase):
    def test_every_route_is_under_api_v1_or_a_known_non_api_prefix(self):
        stray = [route for route in _routes(get_resolver().url_patterns) if not route.startswith(UNVERSIONED_PREFIXES)]
        self.assertEqual(stray, [], "Put new API routes in config/api_v1.py so they are served under /api/v1/.")

    def test_the_storefront_pages_have_no_json_endpoints_left(self):
        storefront = [route for route in _routes(get_resolver().url_patterns) if route.startswith("s/")]
        for moved in ("cart/add", "cart/change", "cart/clear", "cart/fragment", "_track", "newsletter", "wishlist/items"):
            self.assertFalse([r for r in storefront if f"/{moved}" in r], moved)

    def test_health_checks_are_versioned(self):
        routes = set(_routes(get_resolver().url_patterns))
        self.assertLessEqual({"api/v1/health/live/", "api/v1/health/ready/", "api/v1/health/jobs/"}, routes)
