from rest_framework.routers import SimpleRouter

from .views import PurchaseOrderViewSet

router = SimpleRouter()
router.register("", PurchaseOrderViewSet, basename="purchase-order")

urlpatterns = router.urls
