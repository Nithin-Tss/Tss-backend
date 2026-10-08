from rest_framework.routers import SimpleRouter

from .views import CollectionViewSet, ProductViewSet

router = SimpleRouter()
router.register("products", ProductViewSet, basename="product")
router.register("collections", CollectionViewSet, basename="collection")

urlpatterns = router.urls
