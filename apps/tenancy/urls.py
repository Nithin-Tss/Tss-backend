from rest_framework.routers import SimpleRouter

from .views import StoreViewSet

# SimpleRouter, not DefaultRouter: its API-root page would take over the empty "" prefix.
router = SimpleRouter()
router.register("", StoreViewSet, basename="store")

urlpatterns = router.urls
