"""
Public, unauthenticated gift card endpoints for shoppers:

  GET  /api/v1/storefront/<store_slug>/gift-cards/options/   currencies and fixed amounts on offer
  POST /api/v1/storefront/<store_slug>/gift-cards/purchase/  start paying for a gift card
  GET  /api/v1/storefront/<store_slug>/gift-cards/balance/?token=...   check a balance (the link the email sends)

Buying a gift card never touches Cart/Checkout: it is a standalone digital purchase, so it can't
be used to buy another gift card, and nothing here needs a shipping address or stock.
"""
from django.core.signing import BadSignature, Signer
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.commerce.storefront_views import StorefrontView

from . import purchases, services
from .currencies import format_amount
from .models import GiftCard, GiftCardCurrencySetting
from .serializers import BalanceCheckSerializer, PurchaseInputSerializer

SIGNER_SALT = "gift-cards.balance-link"


def balance_token(card):
    return Signer(salt=SIGNER_SALT).sign(str(card.pk))


def _card_from_token(store, token):
    try:
        raw_id = Signer(salt=SIGNER_SALT).unsign(token)
    except BadSignature:
        return None
    return GiftCard.objects.filter(store=store, pk=raw_id, is_deleted=False).first()


class GiftCardOptionsView(StorefrontView):
    def get(self, request, store_slug):
        settings_qs = GiftCardCurrencySetting.objects.filter(store=self.store, is_enabled=True).order_by("currency_code")
        if not settings_qs.exists():
            services.seed_default_currency(self.store)
            settings_qs = GiftCardCurrencySetting.objects.filter(store=self.store, is_enabled=True).order_by("currency_code")
        return Response([
            {
                "currency_code": s.currency_code,
                "amounts": [
                    {"value": a, "display": format_amount(s.currency_code, a)} for a in s.amounts
                ],
            }
            for s in settings_qs
        ])


class GiftCardPurchaseView(StorefrontView):
    throttle_scope = "storefront_checkout"

    def post(self, request, store_slug):
        data = PurchaseInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        result = purchases.create_purchase_intent(self.store, **data.validated_data)
        return Response(result, status=201)


class GiftCardBalanceView(StorefrontView):
    """
    Deliberately NOT a "type in your code" balance checker: the only way in is the signed link
    the purchase/resend email sends, so a brute-force guess of a code can't be used to probe
    balances. Rate limited the same as the rest of the storefront API.
    """

    def get(self, request, store_slug):
        data = BalanceCheckSerializer(data=request.query_params)
        data.is_valid(raise_exception=True)
        card = _card_from_token(self.store, data.validated_data["token"])
        if card is None:
            raise ValidationError("This balance link is invalid.")
        return Response({
            "code_last4": card.code_last4,
            "currency_code": card.currency_code,
            "current_balance": str(card.current_balance),
            "current_balance_display": format_amount(card.currency_code, card.current_balance),
            "status": card.status,
            "expires_at": card.expires_at,
        })
