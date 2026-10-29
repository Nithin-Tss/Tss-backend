from rest_framework.routers import SimpleRouter

from .views import WebhookDeliveryViewSet, WebhookEndpointViewSet

router = SimpleRouter()
router.register("endpoints", WebhookEndpointViewSet, basename="webhook-endpoint")
router.register("deliveries", WebhookDeliveryViewSet, basename="webhook-delivery")

urlpatterns = router.urls
