from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet

from . import services
from .models import GiftCard
from .serializers import AdjustSerializer, GiftCardSerializer


class GiftCardViewSet(StoreScopedViewSet):
    """
    /api/v1/gift-cards/                     list, issue, retrieve, update (status, customer, expiry)
    POST /api/v1/gift-cards/<id>/adjust/    {amount, reason}: negative spends, positive credits back
    """

    queryset = GiftCard.objects.prefetch_related("transactions__created_by_user").order_by("-created_at")
    serializer_class = GiftCardSerializer

    def perform_create(self, serializer):
        store = self.request.store
        code = serializer.validated_data.get("code") or services.generate_code(store)
        serializer.save(
            store=store,
            code=code,
            current_balance=serializer.validated_data["initial_balance"],
            is_deleted=False,
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
