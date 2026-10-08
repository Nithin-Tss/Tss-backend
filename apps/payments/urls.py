from rest_framework.routers import SimpleRouter

from .views import PaymentViewSet

router = SimpleRouter()
router.register("", PaymentViewSet, basename="payment")

urlpatterns = router.urls
