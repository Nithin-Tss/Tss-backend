"""
Public cart and checkout API for shoppers. No sign-in: a cart belongs to the
`session_id` the storefront sends (X-Cart-Session header).

/api/v1/storefront/<store_slug>/cart/                 GET the cart, DELETE to empty it
/api/v1/storefront/<store_slug>/cart/items/           POST {product, variant?, quantity}
/api/v1/storefront/<store_slug>/cart/items/<id>/      PATCH {quantity} (0 removes)
/api/v1/storefront/<store_slug>/checkout/             POST {email?, shipping_address?, billing_address?}
/api/v1/storefront/<store_slug>/checkout/<id>/        GET, PATCH
"""
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product, ProductVariant
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

    def cart(self, request):
        session_id = request.headers.get(SESSION_HEADER)
        if not session_id:
            raise ValidationError(f"{SESSION_HEADER} header is required.")
        return services.get_or_create_cart(self.store, session_id=session_id)


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
        # Only the session that owns the cart may see its checkout.
        cart = self.cart(request)
        checkout = Checkout.objects.filter(pk=checkout_id, cart=cart, is_deleted=False).first()
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
