from rest_framework.routers import SimpleRouter

from .views import WishlistItemViewSet, WishlistViewSet

router = SimpleRouter()
router.register("items", WishlistItemViewSet, basename="wishlist-item")
router.register("", WishlistViewSet, basename="wishlist")

urlpatterns = router.urls
