from rest_framework.routers import SimpleRouter

from .views import CustomerAddressViewSet, CustomerViewSet

# "addresses" first: otherwise "addresses" would be read as a customer id.
router = SimpleRouter()
router.register("addresses", CustomerAddressViewSet, basename="customer-address")
router.register("", CustomerViewSet, basename="customer")

urlpatterns = router.urls
