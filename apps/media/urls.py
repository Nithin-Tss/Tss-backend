from rest_framework.routers import SimpleRouter

from .views import MediaAssetViewSet

router = SimpleRouter()
router.register("", MediaAssetViewSet, basename="media-asset")

urlpatterns = router.urls
