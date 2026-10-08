from rest_framework.routers import SimpleRouter

from .views import CartViewSet, CheckoutViewSet

router = SimpleRouter()
router.register("carts", CartViewSet, basename="cart")
router.register("checkouts", CheckoutViewSet, basename="checkout")

urlpatterns = router.urls
