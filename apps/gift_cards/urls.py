from rest_framework.routers import SimpleRouter

from .views import GiftCardViewSet

router = SimpleRouter()
router.register("", GiftCardViewSet, basename="gift-card")

urlpatterns = router.urls
