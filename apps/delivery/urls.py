from rest_framework.routers import SimpleRouter

from .views import DeliveryGatewayViewSet, ShipmentViewSet, ShippingRateViewSet

router = SimpleRouter()
router.register("gateways", DeliveryGatewayViewSet, basename="delivery-gateway")
router.register("rates", ShippingRateViewSet, basename="shipping-rate")
router.register("shipments", ShipmentViewSet, basename="shipment")

urlpatterns = router.urls
