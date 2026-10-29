from . import services

API_PREFIX = "/api/"


class AuditMiddleware:
    """
    Writes the audit entry for a store API request after its response is known.
    The request is registered for auditing by IsStoreMember (see services.begin).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path_info.startswith(API_PREFIX):
            return self.get_response(request)

        try:
            response = self.get_response(request)
            services.log_request(request, response)
            return response
        finally:
            services.end()
