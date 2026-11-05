from django.urls import path
from rest_framework.routers import SimpleRouter

from .stripe_views import BankPayoutView, StoreStripeWebhookView, StripeConnectView, StripeWebhookView
from .views import PaymentViewSet

router = SimpleRouter()
router.register("", PaymentViewSet, basename="payment")

urlpatterns = [
    path("stripe/connect/", StripeConnectView.as_view(), name="stripe-connect"),
    path("bank-payout/", BankPayoutView.as_view(), name="bank-payout"),
    path("stripe/webhook/<uuid:store_id>/", StoreStripeWebhookView.as_view(), name="stripe-store-webhook"),
    path("stripe/webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
    *router.urls,
]
