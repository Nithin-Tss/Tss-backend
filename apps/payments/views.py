from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet
from apps.orders.models import Order
from apps.orders.services import CANCELLED

from . import services
from .models import Payment
from .serializers import PaymentSerializer, RecordPaymentSerializer, RefundSerializer


class PaymentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    StoreScopedViewSet,
):
    """
    /api/v1/payments/?order=<id>             list, retrieve
    POST /api/v1/payments/                   record a manual payment {order, amount, reference}
    POST /api/v1/payments/<id>/refund/       {amount, reason}
    Payments are never edited or deleted - refund instead.
    """

    queryset = Payment.objects.prefetch_related("transactions").order_by("-created_at")
    serializer_class = PaymentSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("order"):
            qs = qs.filter(order_id=self.request.query_params["order"])
        return qs

    def create(self, request, *args, **kwargs):
        data = RecordPaymentSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data

        order = Order.objects.filter(store=request.store, pk=v["order"]).first()
        if order is None:
            raise ValidationError({"order": "No such order in this store."})
        if order.status == CANCELLED:
            raise ValidationError({"order": "This order is cancelled."})

        due = order.total - services.paid_total(order)
        if v["amount"] > due:
            raise ValidationError({"amount": f"Only {due} is still due on this order."})

        payment = services.record_payment(
            request.store, order, v["amount"], provider=v["provider"], reference=v.get("reference") or None
        )
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        data = RefundSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        payment = services.refund(self.get_object(), data.validated_data["amount"], data.validated_data.get("reason"))
        return Response(PaymentSerializer(self.get_queryset().get(pk=payment.pk)).data)
