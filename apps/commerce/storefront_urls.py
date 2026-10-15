"""Public shopper cart and checkout API."""
from django.urls import path

from .storefront_views import (
    CartItemsView, CartItemView, CartView, CheckoutCompleteView, CheckoutDetailView, CheckoutStartView,
    OrderCancelView, OrderDetailView, OrderListView,
)

urlpatterns = [
    path("<slug:store_slug>/cart/", CartView.as_view(), name="storefront-cart"),
    path("<slug:store_slug>/cart/items/", CartItemsView.as_view(), name="storefront-cart-items"),
    path("<slug:store_slug>/cart/items/<uuid:item_id>/", CartItemView.as_view(), name="storefront-cart-item"),
    path("<slug:store_slug>/checkout/", CheckoutStartView.as_view(), name="storefront-checkout"),
    path("<slug:store_slug>/checkout/<uuid:checkout_id>/", CheckoutDetailView.as_view(), name="storefront-checkout-detail"),
    path("<slug:store_slug>/checkout/<uuid:checkout_id>/complete/", CheckoutCompleteView.as_view(), name="storefront-checkout-complete"),
    path("<slug:store_slug>/orders/", OrderListView.as_view(), name="storefront-orders"),
    path("<slug:store_slug>/orders/<uuid:order_id>/", OrderDetailView.as_view(), name="storefront-order"),
    path("<slug:store_slug>/orders/<uuid:order_id>/cancel/", OrderCancelView.as_view(), name="storefront-order-cancel"),
]
