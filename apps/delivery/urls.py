from rest_framework.routers import SimpleRouter

from .views import DeliveryGatewayViewSet, ShipmentViewSet

router = SimpleRouter()
router.register("gateways", DeliveryGatewayViewSet, basename="delivery-gateway")
router.register("shipments", ShipmentViewSet, basename="shipment")

urlpatterns = router.urls
