"""
Serve each store on its own hostname.

A request whose Host is a store's `domain` is rewritten to that store's storefront
(/s/<store_slug>/...), so "my-shop.example.com/collections/all" is handled exactly like
"/s/my-shop/collections/all". The response is rewritten the other way, so the visitor
never sees the /s/<store_slug> prefix: links in the body, redirects and cookie paths all
use the clean URL. Other hosts, and the API/admin on a store host, are untouched.
"""
import re

from .models import Store
from .services import ACTIVE

STORE_PREFIX = "/s/"
PASSTHROUGH = ("/api/", "/admin/", "/static/", "/s/", "/healthz", "/readyz")
REWRITABLE_TYPES = ("text/html", "text/xml", "application/xml", "application/json", "text/javascript", "application/javascript")


def _prefix_pattern(slug):
    # "/s/<slug>" followed by "/" or the end of a URL, never part of a longer slug
    return re.compile(rf"/s/{re.escape(slug)}(?=[/?#\"'\s<)\\]|$)")


class StoreDomainMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":")[0].lower()
        path = request.path_info

        if path.startswith(PASSTHROUGH):
            return self.get_response(request)

        slug = (
            Store.objects.filter(domain=host, status=ACTIVE)
            .values_list("store_slug", flat=True)
            .first()
        )
        if not slug:
            return self.get_response(request)

        request.path_info = f"{STORE_PREFIX}{slug}{path}"
        request.path = request.path_info
        return self._clean(self.get_response(request), slug)

    def _clean(self, response, slug):
        pattern = _prefix_pattern(slug)
        prefix = f"{STORE_PREFIX}{slug}"

        location = response.get("Location")
        if location:
            response["Location"] = pattern.sub("", location) or "/"

        for cookie in response.cookies.values():
            if cookie["path"].startswith(prefix):
                cookie["path"] = cookie["path"][len(prefix):] or "/"

        content_type = response.get("Content-Type", "")
        if (
            not response.streaming
            and not response.has_header("Content-Encoding")
            and content_type.startswith(REWRITABLE_TYPES)
        ):
            charset = getattr(response, "charset", None) or "utf-8"
            body = response.content.decode(charset)
            # A bare "/s/x" is the site root; "/s/x/blog" is "/blog"
            body = re.sub(rf"{re.escape(prefix)}(?=[\"'])", "/", body)
            response.content = pattern.sub("", body).encode(charset)
        return response
