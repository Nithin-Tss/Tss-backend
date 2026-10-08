from django.db import transaction
from django.db.models import Q

from apps.core.viewsets import StoreScopedViewSet

from .models import Customer, CustomerAddress
from .serializers import CustomerAddressSerializer, CustomerSerializer


class CustomerViewSet(StoreScopedViewSet):
    """/api/v1/customers/  ?search=name or email"""

    queryset = Customer.objects.prefetch_related("addresses").order_by("-created_at")
    serializer_class = CustomerSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )
        return qs


class CustomerAddressViewSet(StoreScopedViewSet):
    """/api/v1/customers/addresses/  ?customer=<id>"""

    queryset = CustomerAddress.objects.order_by("-is_default", "created_at")
    serializer_class = CustomerAddressSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("customer"):
            qs = qs.filter(customer=self.request.query_params["customer"])
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        address = serializer.save()
        self._one_default(address)

    @transaction.atomic
    def perform_update(self, serializer):
        address = serializer.save()
        self._one_default(address)

    def _one_default(self, address):
        """A customer has at most one default address per type."""
        if address.is_default:
            CustomerAddress.objects.filter(
                customer=address.customer, address_type=address.address_type
            ).exclude(pk=address.pk).update(is_default=False)
