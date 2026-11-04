"""
Request validation and security headers for the JSON API (paths under /api/).

- RequestValidationMiddleware rejects bad requests before any view or parser runs:
  null bytes in the URL, bodies over the size limit, and body types the API does not read.
- SecurityHeadersMiddleware adds hardening headers to API responses.

Both leave the public storefront (/s/...) and /admin/ alone: those render HTML and the
storefront cart does its own CORS and content handling.
"""
from django.conf import settings
from django.http import JsonResponse

API_PREFIX = "/api/"
# Swagger UI and ReDoc load scripts and styles from a CDN and run inline code, so the
# strict "nothing may load" policy would leave them blank. Only these two pages get this.
DOCS_PATHS = ("/api/v1/docs/", "/api/v1/redoc/")
DOCS_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https:; font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'"
)
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_api(request):
    return request.path_info.startswith(API_PREFIX)


def _error(status, detail):
    return JsonResponse({"detail": detail}, status=status)


class RequestValidationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _is_api(request):
            rejected = self.validate(request)
            if rejected:
                return rejected
        return self.get_response(request)

    def validate(self, request):
        # get_full_path() re-encodes the path, so a decoded null byte has to be looked for in path_info
        if "\x00" in request.path_info or "%00" in request.META.get("QUERY_STRING", ""):
            return _error(400, "Invalid characters in the request URL.")

        length = request.META.get("CONTENT_LENGTH") or ""
        if length:
            if not length.isdigit():
                return _error(400, "Invalid Content-Length header.")
            if int(length) > settings.API_MAX_REQUEST_BYTES:
                return _error(413, "Request body is too large.")

        content_type = request.META.get("CONTENT_TYPE", "").split(";")[0].strip().lower()
        # A chunked upload has no declared size, so the limit above can't apply to it
        if request.method in WRITE_METHODS and content_type == "multipart/form-data" and not length:
            return _error(411, "Uploads must declare their size (Content-Length).")

        if request.method in WRITE_METHODS and (length not in ("", "0")):
            if content_type not in settings.API_ALLOWED_CONTENT_TYPES:
                return _error(415, "Unsupported content type.")
        return None


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if _is_api(request):
            # The API only returns JSON/streams: never render, frame or embed it.
            if request.path_info in DOCS_PATHS:
                response.headers["Content-Security-Policy"] = DOCS_CSP
            else:
                response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
            # Responses carry account and order data: keep them out of shared caches.
            response.headers.setdefault("Cache-Control", "no-store")
        return response
