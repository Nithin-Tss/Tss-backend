"""
JSON error pages for the API. Paths under /api/ get {"detail": ...} instead of Django's
HTML 404/500 pages; every other path keeps the default pages.
"""
from django.http import JsonResponse
from django.views import defaults

from .middleware import API_PREFIX


def _is_api(request):
    return request.path_info.startswith(API_PREFIX)


def not_found(request, exception, template_name="404.html"):
    if _is_api(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    return defaults.page_not_found(request, exception, template_name)


def server_error(request, template_name="500.html"):
    if _is_api(request):
        return JsonResponse({"detail": "Server error."}, status=500)
    return defaults.server_error(request, template_name)
