from rest_framework.routers import SimpleRouter

from .views import FulfillmentViewSet

router = SimpleRouter()
router.register("", FulfillmentViewSet, basename="fulfillment")

urlpatterns = router.urls
