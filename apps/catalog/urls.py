from rest_framework.routers import SimpleRouter

from .views import CollectionViewSet, ProductViewSet, VariantViewSet

router = SimpleRouter()
router.register("products", ProductViewSet, basename="product")
router.register("variants", VariantViewSet, basename="variant")
router.register("collections", CollectionViewSet, basename="collection")

urlpatterns = router.urls
