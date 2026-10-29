from rest_framework.routers import SimpleRouter

from .views import InventoryItemViewSet, LocationViewSet, StockReservationViewSet, TransferViewSet

router = SimpleRouter()
router.register("locations", LocationViewSet, basename="location")
router.register("items", InventoryItemViewSet, basename="inventory-item")
router.register("reservations", StockReservationViewSet, basename="stock-reservation")
router.register("transfers", TransferViewSet, basename="transfer")

urlpatterns = router.urls
