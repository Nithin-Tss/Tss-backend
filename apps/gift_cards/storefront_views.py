"""
Public, unauthenticated gift card endpoint for shoppers:

  GET /api/v1/storefront/<store_slug>/gift-cards/balance/?token=...   check a balance (the link the email sends)

Buying a gift card is not here: it is a normal product, bought through the normal cart and
checkout (see apps.commerce.services and apps.gift_cards.purchases).
"""
from django.core.signing import BadSignature, Signer
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.commerce.storefront_views import StorefrontView

from .currencies import format_amount, quantize
from .models import GiftCard
from .serializers import BalanceCheckSerializer

SIGNER_SALT = "gift-cards.balance-link"


def balance_token(card):
    return Signer(salt=SIGNER_SALT).sign(str(card.pk))


def _card_from_token(store, token):
    try:
        raw_id = Signer(salt=SIGNER_SALT).unsign(token)
    except BadSignature:
        return None
    return GiftCard.objects.filter(store=store, pk=raw_id, is_deleted=False).first()


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
            "current_balance": str(quantize(card.currency_code, card.current_balance)),
            "current_balance_display": format_amount(card.currency_code, card.current_balance),
            "status": card.status,
            "expires_at": card.expires_at,
        })
