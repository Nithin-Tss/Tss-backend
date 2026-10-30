from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedGenericViewSet, StoreScopedViewSet

from . import events, services
from .models import FAILED, SUCCEEDED, WebhookDelivery, WebhookEndpoint
from .serializers import WebhookDeliverySerializer, WebhookEndpointSerializer

# The area only has "manage": the signing secrets are in here, so reading is as restricted as writing.
READ_NEEDS_MANAGE = {"GET": "webhooks.manage"}


class WebhookEndpointViewSet(StoreScopedViewSet):
    """
    /api/v1/webhooks/endpoints/
    The signing secret is shown only when the endpoint is created and by rotate-secret.
    """

    permission_resource = "webhooks"
    action_permissions = READ_NEEDS_MANAGE
    queryset = WebhookEndpoint.objects.order_by("-created_at")
    serializer_class = WebhookEndpointSerializer

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        response.data["secret"] = WebhookEndpoint.objects.get(pk=response.data["id"]).secret
        return response

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        endpoint = self.get_object()
        services.rotate_secret(endpoint)
        return Response({
            "secret": endpoint.secret,
            "previous_secret_valid_until": endpoint.previous_secret_expires_at,
        })

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        """Queues a "ping" event to this endpoint."""
        delivery = services.queue_ping(self.get_object())
        return Response(WebhookDeliverySerializer(delivery).data, status=202)

    @action(detail=False, methods=["get"], url_path="events")
    def event_types(self, request):
        return Response([{"type": k, "description": v} for k, v in events.EVENTS.items()])


class WebhookDeliveryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, StoreScopedGenericViewSet):
    """/api/v1/webhooks/deliveries/  ?endpoint=<id>&status=pending|succeeded|failed&event_type=..."""

    permission_resource = "webhooks"
    action_permissions = {**READ_NEEDS_MANAGE}
    queryset = WebhookDelivery.objects.order_by("-created_at")
    serializer_class = WebhookDeliverySerializer

    def get_queryset(self):
        qs = super().get_queryset()
        for name in ("endpoint", "status", "event_type"):
            if self.request.query_params.get(name):
                qs = qs.filter(**{name: self.request.query_params[name]})
        return qs

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        delivery = self.get_object()
        if delivery.status not in (SUCCEEDED, FAILED):
            raise ValidationError("This delivery is still waiting to be sent.")
        services.retry(delivery)
        return Response(self.get_serializer(delivery).data, status=202)
