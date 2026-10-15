"""
Public cart and checkout API for shoppers. No sign-in: a cart belongs to the
`session_id` the storefront sends (X-Cart-Session header).

/api/v1/storefront/<store_slug>/cart/                 GET the cart, DELETE to empty it
/api/v1/storefront/<store_slug>/cart/items/           POST {product, variant?, quantity}
/api/v1/storefront/<store_slug>/cart/items/<id>/      PATCH {quantity} (0 removes)
/api/v1/storefront/<store_slug>/checkout/             POST {email?, shipping_address?, billing_address?, discount_code?}
/api/v1/storefront/<store_slug>/checkout/<id>/        GET, PATCH ("discount_code": "" removes the code)
/api/v1/storefront/<store_slug>/checkout/<id>/complete/   POST: place the order (repeat-safe)
/api/v1/storefront/<store_slug>/orders/               GET orders placed from this session's carts
/api/v1/storefront/<store_slug>/orders/<id>/          GET
/api/v1/storefront/<store_slug>/orders/<id>/cancel/   POST: cancel, stock goes back
"""
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product, ProductVariant
from apps.orders import services as orders_services
from apps.orders.models import Order
from apps.orders.serializers import OrderSerializer
from apps.tenancy.models import Store

from . import services
from .models import Checkout
from .serializers import CartSerializer, CheckoutSerializer

SESSION_HEADER = "X-Cart-Session"
STORE_ACTIVE = "active"


class StorefrontView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.store = Store.objects.filter(store_slug=kwargs["store_slug"], status=STORE_ACTIVE).first()
        if self.store is None:
            raise Http404("Store not found.")

    def session_id(self, request):
        session_id = request.headers.get(SESSION_HEADER)
        if not session_id:
            raise ValidationError(f"{SESSION_HEADER} header is required.")
        return session_id

    def cart(self, request):
        return services.get_or_create_cart(self.store, session_id=self.session_id(request))


class AddItemSerializer(serializers.Serializer):
    product = serializers.UUIDField()
    variant = serializers.UUIDField(required=False, allow_null=True)
    quantity = serializers.IntegerField(min_value=1, default=1)


class QuantitySerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=0)


class CheckoutInputSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)
    shipping_address = serializers.JSONField(required=False)
    billing_address = serializers.JSONField(required=False)
    discount_code = serializers.CharField(required=False, allow_blank=True, max_length=50)


class CartView(StorefrontView):
    def get(self, request, store_slug):
        return Response(CartSerializer(self.cart(request)).data)

    def delete(self, request, store_slug):
        return Response(CartSerializer(services.clear_cart(self.cart(request))).data)


class CartItemsView(StorefrontView):
    def post(self, request, store_slug):
        data = AddItemSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data

        product = Product.objects.filter(store=self.store, pk=v["product"]).first()
        if product is None:
            raise ValidationError({"product": "This product isn't available."})

        variant = None
        if v.get("variant"):
            variant = ProductVariant.objects.filter(product=product, pk=v["variant"]).first()
            if variant is None:
                raise ValidationError({"variant": f"Doesn't belong to {product.title}."})

        cart = self.cart(request)
        services.add_item(cart, product, v["quantity"], variant=variant)
        return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)


class CartItemView(StorefrontView):
    def patch(self, request, store_slug, item_id):
        data = QuantitySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        cart = self.cart(request)
        services.set_quantity(cart, item_id, data.validated_data["quantity"])
        return Response(CartSerializer(cart).data)


class CheckoutStartView(StorefrontView):
    def post(self, request, store_slug):
        data = CheckoutInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        checkout = services.start_checkout(self.cart(request), **data.validated_data)
        return Response(CheckoutSerializer(checkout).data, status=status.HTTP_201_CREATED)


class CheckoutDetailView(StorefrontView):
    def _checkout(self, request, checkout_id):
        # Only the session that owns the cart may see its checkout. Matched by
        # session (not the open cart) so a completed checkout stays reachable.
        checkout = Checkout.objects.filter(
            pk=checkout_id, store=self.store, cart__session_id=self.session_id(request), is_deleted=False
        ).first()
        if checkout is None:
            raise Http404("Checkout not found.")
        return checkout

    def get(self, request, store_slug, checkout_id):
        return Response(CheckoutSerializer(self._checkout(request, checkout_id)).data)

    def patch(self, request, store_slug, checkout_id):
        checkout = self._checkout(request, checkout_id)
        data = CheckoutInputSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        return Response(CheckoutSerializer(services.update_checkout(checkout, **data.validated_data)).data)


class CheckoutCompleteView(CheckoutDetailView):
    http_method_names = ["post", "options"]

    def post(self, request, store_slug, checkout_id):
        order = services.place_order(self._checkout(request, checkout_id))
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


class SessionOrdersMixin:
    """Orders placed from this session's carts: the shopper's only way to find them."""

    def session_orders(self, request):
        order_ids = [
            (c.metadata or {}).get("order_id")
            for c in Checkout.objects.filter(
                cart__session_id=self.session_id(request), store=self.store, is_deleted=False
            )
        ]
        return Order.objects.filter(store=self.store, pk__in=[i for i in order_ids if i]).prefetch_related("items")

    def session_order(self, request, order_id):
        order = self.session_orders(request).filter(pk=order_id).first()
        if order is None:
            raise Http404("Order not found.")
        return order


class OrderListView(SessionOrdersMixin, StorefrontView):
    def get(self, request, store_slug):
        orders = self.session_orders(request).order_by("-created_at")
        return Response(OrderSerializer(orders, many=True).data)


class OrderDetailView(SessionOrdersMixin, StorefrontView):
    def get(self, request, store_slug, order_id):
        return Response(OrderSerializer(self.session_order(request, order_id)).data)


class OrderCancelView(SessionOrdersMixin, StorefrontView):
    def post(self, request, store_slug, order_id):
        # Puts the stock back; refused once items have shipped.
        order = orders_services.cancel_order(self.session_order(request, order_id))
        return Response(OrderSerializer(order).data)
