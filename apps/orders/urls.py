from rest_framework.routers import SimpleRouter

from .views import DraftOrderViewSet, OrderViewSet

# "drafts" first: otherwise "drafts" would be read as an order id.
router = SimpleRouter()
router.register("drafts", DraftOrderViewSet, basename="draft-order")
router.register("", OrderViewSet, basename="order")

urlpatterns = router.urls
