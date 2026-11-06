from rest_framework.routers import SimpleRouter

from .views import GiftCardCurrencySettingViewSet, GiftCardViewSet

router = SimpleRouter()
router.register("currencies", GiftCardCurrencySettingViewSet, basename="gift-card-currency")
router.register("", GiftCardViewSet, basename="gift-card")

urlpatterns = router.urls
