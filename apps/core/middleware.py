from django.middleware.gzip import GZipMiddleware


class ApiGZipMiddleware(GZipMiddleware):
    """
    gzip for JSON/HTML responses (typically 5-10x smaller on large lists).
    Server-Sent Events streams are left alone so events aren't buffered.
    """

    def process_response(self, request, response):
        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            return response
        return super().process_response(request, response)
