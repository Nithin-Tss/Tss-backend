import csv

from django.db.models import Q, Sum
from django.http import HttpResponse
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet

from . import emails, services
from .currencies import format_amount
from .models import GiftCard, GiftCardCurrencySetting
from .services import ACTIVE
from .serializers import (
    AdjustSerializer, CreateGiftCardSerializer, GiftCardCurrencySettingSerializer, GiftCardSerializer,
    ResendSerializer,
)


class GiftCardViewSet(StoreScopedViewSet):
    """
    /api/v1/gift-cards/                     list (?search=, ?status=, ?currency_code=, ?ordering=),
                                             create a free gift card, retrieve, update (status, customer, expiry)
    POST /api/v1/gift-cards/<id>/adjust/     {amount, reason}: negative spends, positive credits back
    POST /api/v1/gift-cards/<id>/resend/     {email?}: re-email the code
    POST /api/v1/gift-cards/<id>/deactivate/ {reason?}: disable; cannot be undone
    GET  /api/v1/gift-cards/export/          CSV of the current filters, codes masked to the last 4
    GET  /api/v1/gift-cards/report/          totals grouped by currency: issued, redeemed, outstanding
    """

    permission_resource = "gift_cards"
    queryset = GiftCard.objects.select_related("customer").prefetch_related(
        "transactions__created_by_user"
    ).order_by("-created_at")
    serializer_class = GiftCardSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        search = params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(code_last4__icontains=search.lstrip("*").strip())
                | Q(customer__email__icontains=search)
                | Q(customer__first_name__icontains=search)
                | Q(customer__last_name__icontains=search)
            )
        status_param = params.get("status", "").strip()
        if status_param:
            qs = qs.filter(status=status_param)
        currency = params.get("currency_code", "").strip()
        if currency:
            qs = qs.filter(currency_code=currency.upper())

        ordering = params.get("ordering", "").strip()
        allowed = {"created_at", "-created_at", "current_balance", "-current_balance", "initial_balance", "-initial_balance"}
        if ordering in allowed:
            qs = qs.order_by(ordering)
        return qs

    def create(self, request, *args, **kwargs):
        """Issue a free gift card. The raw code is returned here once; it is never shown again."""
        data = CreateGiftCardSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        store = request.store
        currency_code = data.validated_data.get("currency_code") or self._default_currency(store)

        customer = None
        if data.validated_data.get("customer"):
            from apps.customers.models import Customer

            customer = Customer.objects.filter(store=store, pk=data.validated_data["customer"]).first()
            if customer is None:
                raise ValidationError({"customer": "No such customer."})

        code = services.generate_code(store)
        card = GiftCard.objects.create(
            store=store,
            code_hash=code["code_hash"],
            code_enc=code["code_enc"],
            code_last4=code["code_last4"],
            initial_balance=data.validated_data["initial_balance"],
            current_balance=data.validated_data["initial_balance"],
            currency_code=currency_code,
            status=ACTIVE,
            customer=customer,
            expires_at=data.validated_data.get("expires_at"),
            is_deleted=False,
        )
        from .models import GiftCardTransaction

        GiftCardTransaction.objects.create(
            gift_card=card,
            transaction_type="credit",
            amount=card.initial_balance,
            balance_before=0,
            balance_after=card.initial_balance,
            created_by_user=request.user,
            reason=data.validated_data.get("note") or "Issued by staff",
        )
        payload = self.get_serializer(card).data
        payload["code"] = code["raw"]  # shown once; the serializer itself never includes it
        return Response(payload, status=201)

    @staticmethod
    def _default_currency(store):
        from .currencies import DEFAULT_CURRENCY

        return (
            GiftCardCurrencySetting.objects.filter(store=store, is_enabled=True).values_list("currency_code", flat=True).first()
            or DEFAULT_CURRENCY
        )

    @action(detail=True, methods=["post"])
    def adjust(self, request, pk=None):
        data = AdjustSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        amount = data.validated_data["amount"]
        card = services.change_balance(
            self.get_object(),
            amount,
            kind="credit" if amount > 0 else "debit",
            user=request.user,
            reason=data.validated_data.get("reason") or "Manual adjustment",
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=card.pk)).data)

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        card = self.get_object()
        data = ResendSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        recipient = data.validated_data.get("email") or (card.customer.email if card.customer else None)
        if not recipient:
            raise ValidationError({"email": "This gift card has no customer email on file; give one to send to."})
        raw_code = services.reveal_code(card)
        if not raw_code:
            raise ValidationError("This gift card's code can't be recovered; issue a new one instead.")
        emails.gift_card_resent(card, raw_code, recipient)
        return Response({"sent_to": recipient})

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        card = services.deactivate(self.get_object(), user=request.user, reason=request.data.get("reason"))
        return Response(self.get_serializer(card).data)

    @action(detail=False, methods=["get"])
    def export(self, request):
        """CSV of the filtered list. Codes are masked to the last 4 characters, same as the admin UI."""
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="gift-cards.csv"'
        writer = csv.writer(response)
        writer.writerow(["Code", "Currency", "Initial balance", "Current balance", "Status", "Customer", "Expires", "Created"])
        for card in self.get_queryset().select_related("customer"):
            writer.writerow([
                f"****{card.code_last4}",
                card.currency_code,
                card.initial_balance,
                card.current_balance,
                card.status,
                card.customer.email if card.customer else "",
                card.expires_at.date().isoformat() if card.expires_at else "Never",
                card.created_at.date().isoformat(),
            ])
        return response

    @action(detail=False, methods=["get"])
    def report(self, request):
        """Sold, redeemed and outstanding balance, grouped by currency -- never summed across currencies."""
        rows = (
            GiftCard.objects.filter(store=request.store, is_deleted=False)
            .values("currency_code")
            .annotate(issued=Sum("initial_balance"), outstanding=Sum("current_balance"))
            .order_by("currency_code")
        )
        result = []
        for row in rows:
            issued = row["issued"] or 0
            outstanding = row["outstanding"] or 0
            result.append({
                "currency_code": row["currency_code"],
                "issued": str(issued),
                "issued_display": format_amount(row["currency_code"], issued),
                "redeemed": str(issued - outstanding),
                "redeemed_display": format_amount(row["currency_code"], issued - outstanding),
                "outstanding": str(outstanding),
                "outstanding_display": format_amount(row["currency_code"], outstanding),
            })
        return Response(result)


class GiftCardCurrencySettingViewSet(StoreScopedViewSet):
    """
    /api/v1/gift-cards/currencies/   list/create/update which currencies this store sells gift
    cards in, and the fixed amounts offered for each. Defaults to AUD 25/50/100/200 if untouched.
    """

    permission_resource = "gift_cards"
    queryset = GiftCardCurrencySetting.objects.order_by("currency_code")
    serializer_class = GiftCardCurrencySettingSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if not qs.exists():
            services.seed_default_currency(self.request.store)
            qs = super().get_queryset()
        return qs
