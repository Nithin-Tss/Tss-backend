from rest_framework.routers import SimpleRouter

from .views import CategoryViewSet, CollectionViewSet, ProductViewSet, SalesChannelViewSet, VariantViewSet

router = SimpleRouter()
router.register("products", ProductViewSet, basename="product")
router.register("variants", VariantViewSet, basename="variant")
router.register("collections", CollectionViewSet, basename="collection")
router.register("categories", CategoryViewSet, basename="category")
router.register("sales-channels", SalesChannelViewSet, basename="sales-channel")

urlpatterns = router.urls
