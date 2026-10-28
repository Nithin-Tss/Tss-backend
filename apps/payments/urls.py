from django.urls import path
from rest_framework.routers import SimpleRouter

from .stripe_views import StripeConnectView, StripeWebhookView
from .views import PaymentViewSet

router = SimpleRouter()
router.register("", PaymentViewSet, basename="payment")

urlpatterns = [
    path("stripe/connect/", StripeConnectView.as_view(), name="stripe-connect"),
    path("stripe/webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
    *router.urls,
]
