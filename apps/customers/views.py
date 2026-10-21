import uuid

from django.db import transaction
from django.db.models import Q
from rest_framework import exceptions, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import ACTIVE, STORE_HEADER
from apps.core.viewsets import StoreScopedViewSet
from apps.tenancy.access import membership_permissions
from apps.tenancy.models import Store, StoreMembership
from apps.tenancy.services import CLOSED, platform_membership

from . import services
from .models import Customer, CustomerAddress
from .serializers import CustomerAddressSerializer, CustomerSerializer


class CustomerViewSet(StoreScopedViewSet):
    """/api/v1/customers/  ?search=name or email"""

    permission_resource = "customers"
    queryset = (
        Customer.objects.prefetch_related("addresses", "orders")
        .order_by("-created_at")
    )
    serializer_class = CustomerSerializer

    def get_queryset(self):
        store = getattr(self.request, "store", None)
        if store:
            from django.contrib.auth import get_user_model
            from django.utils import timezone

            User = get_user_model()
            existing_emails = {
                e.lower()
                for e in Customer.objects.filter(store=store).values_list("email", flat=True)
                if e
            }
            users = User.objects.exclude(email="").exclude(email__isnull=True)
            to_create = []
            now = timezone.now()
            for u in users:
                if u.email and u.email.lower() not in existing_emails:
                    to_create.append(
                        Customer(
                            store=store,
                            email=u.email.lower(),
                            first_name=u.first_name or "",
                            last_name=u.last_name or "",
                            email_subscription_status="subscribed",
                            country="Australia",
                            created_at=now,
                            updated_at=now,
                        )
                    )
            if to_create:
                Customer.objects.bulk_create(to_create, ignore_conflicts=True)

        qs = super().get_queryset()
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )
        return qs


class CustomerAddressViewSet(viewsets.ModelViewSet):
    """
    /api/v1/customers/addresses/
    - For Administrators (with customers.view / customers.manage):
      Can view, add, edit, and delete addresses for any customer in their store.
      Filter by customer: ?customer=<customer_id>
    - For Customers (Profile page):
      Can view, add, edit, delete, and set default only for their own addresses.
      Isolated from other customers' addresses.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = CustomerAddressSerializer
    queryset = CustomerAddress.objects.all()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self._resolve_store_and_access(request)

    def _resolve_store_and_access(self, request):
        """
        Resolves the active store and the signed-in user's role/permissions in that store.

        Staff reach their store through a membership. Shoppers have none, so they can
        only use stores that are open. With no X-Store-Id header we fall back to a store
        the user already belongs to or shops at, never to an arbitrary one.
        """
        raw_store_id = request.headers.get(STORE_HEADER)
        store = None

        if raw_store_id:
            try:
                store_uuid = uuid.UUID(str(raw_store_id).strip())
            except (ValueError, TypeError):
                raise exceptions.ParseError(f"{STORE_HEADER} is not a valid store id.")
            store = Store.objects.filter(pk=store_uuid).first()
        else:
            membership = (
                StoreMembership.objects.select_related("store")
                .filter(user=request.user, status=ACTIVE)
                .order_by("created_at")
                .first()
            )
            if membership:
                store = membership.store
            elif request.user.email_verified_at is not None:
                # Only a verified address may be matched to shopper records.
                existing_cust = (
                    Customer.objects.select_related("store")
                    .filter(email__iexact=request.user.email, store__status=ACTIVE)
                    .order_by("-updated_at")
                    .first()
                )
                if existing_cust:
                    store = existing_cust.store

        if store is None:
            if raw_store_id:
                # Same answer whether the store exists or not, so ids can't be probed.
                raise exceptions.NotFound("Store not found.")
            raise exceptions.ParseError(f"{STORE_HEADER} header is required.")

        membership = (
            StoreMembership.objects.select_related("store")
            .filter(user=request.user, store=store, status=ACTIVE)
            .first()
        )

        if membership is None and request.user.is_owner and store.status != CLOSED:
            membership = platform_membership(request.user, store)

        if membership is None and store.status != ACTIVE:
            raise exceptions.NotFound("Store not found.")

        request.store = store
        request.membership = membership
        request.store_role = membership.role if membership else None

    def _staff_permissions(self, request):
        membership = getattr(request, "membership", None)
        if membership is None:
            return None
        return membership_permissions(membership)

    def can_manage_all(self, request):
        perms = self._staff_permissions(request)
        return perms is not None and "customers.manage" in perms

    def can_view_all(self, request):
        perms = self._staff_permissions(request)
        return perms is not None and (
            "customers.view" in perms or "customers.manage" in perms
        )

    def get_user_customer(self, request):
        """
        Returns or creates the Customer record associated with the logged-in user in this store.
        """
        if not hasattr(request, "_user_customer"):
            if request.user.email_verified_at is None:
                # Customers are matched by email, so the address must be proven first;
                # otherwise anyone could sign up with someone else's email and read their addresses.
                raise exceptions.PermissionDenied("Verify your email address to manage your addresses.")
            email = (request.user.email or "").strip().lower()
            with transaction.atomic():
                # Lock the store row so concurrent first requests don't both create a customer.
                Store.objects.select_for_update().get(pk=request.store.pk)
                customer = (
                    Customer.objects.filter(store=request.store, email__iexact=email)
                    .order_by("created_at")
                    .first()
                )
                if customer is None:
                    customer = Customer.objects.create(
                        store=request.store,
                        email=email,
                        first_name=(request.user.first_name or "").strip(),
                        last_name=(request.user.last_name or "").strip(),
                        email_subscription_status="not_subscribed",
                    )
            request._user_customer = customer

        return request._user_customer

    def get_queryset(self):
        store = self.request.store
        customer_param = self.request.query_params.get("customer", "").strip()

        if customer_param and customer_param != "me":
            try:
                customer_param = str(uuid.UUID(customer_param))
            except ValueError:
                raise exceptions.ParseError("customer must be a valid customer id or 'me'.")

        if customer_param == "me":
            user_cust = self.get_user_customer(self.request)
            return CustomerAddress.objects.filter(
                customer=user_cust, customer__store=store
            ).order_by("-is_default", "-created_at")

        if customer_param:
            if self.can_view_all(self.request):
                return CustomerAddress.objects.filter(
                    customer_id=customer_param, customer__store=store
                ).order_by("-is_default", "-created_at")
            else:
                user_cust = self.get_user_customer(self.request)
                if str(user_cust.id) != customer_param:
                    raise exceptions.PermissionDenied(
                        "You don't have permission to access other customers' addresses."
                    )
                return CustomerAddress.objects.filter(
                    customer=user_cust, customer__store=store
                ).order_by("-is_default", "-created_at")

        # No customer parameter provided
        if self.can_view_all(self.request):
            return CustomerAddress.objects.filter(customer__store=store).order_by(
                "-is_default", "-created_at"
            )

        # Non-admin viewing addresses on their profile
        user_cust = self.get_user_customer(self.request)
        return CustomerAddress.objects.filter(
            customer=user_cust, customer__store=store
        ).order_by("-is_default", "-created_at")

    def perform_create(self, serializer):
        can_manage = self.can_manage_all(self.request)
        provided_customer = serializer.validated_data.get("customer")

        if can_manage:
            if provided_customer:
                if provided_customer.store_id != self.request.store.pk:
                    raise exceptions.ValidationError(
                        {"customer": "Customer does not belong to this store."}
                    )
                services.save_address(serializer, customer=provided_customer)
            else:
                user_cust = self.get_user_customer(self.request)
                services.save_address(serializer, customer=user_cust)
        else:
            user_cust = self.get_user_customer(self.request)
            if provided_customer and provided_customer != user_cust:
                raise exceptions.PermissionDenied(
                    "You can only create addresses for yourself."
                )
            services.save_address(serializer, customer=user_cust)

    def perform_update(self, serializer):
        instance = serializer.instance
        can_manage = self.can_manage_all(self.request)

        if not can_manage:
            user_cust = self.get_user_customer(self.request)
            if instance.customer != user_cust:
                raise exceptions.PermissionDenied(
                    "You can only edit your own addresses."
                )
            if (
                "customer" in serializer.validated_data
                and serializer.validated_data["customer"] != user_cust
            ):
                raise exceptions.PermissionDenied(
                    "You cannot reassign an address to another customer."
                )
        else:
            if "customer" in serializer.validated_data:
                target_cust = serializer.validated_data["customer"]
                if target_cust and target_cust.store_id != self.request.store.pk:
                    raise exceptions.ValidationError(
                        {"customer": "Customer does not belong to this store."}
                    )

        services.save_address(serializer)

    def perform_destroy(self, instance):
        can_manage = self.can_manage_all(self.request)
        if not can_manage:
            user_cust = self.get_user_customer(self.request)
            if instance.customer != user_cust:
                raise exceptions.PermissionDenied(
                    "You can only delete your own addresses."
                )
        services.delete_address(instance)

    @action(detail=True, methods=["post", "patch"], url_path="set-default")
    def set_default(self, request, pk=None):
        """Set this address as default for its type."""
        address = self.get_object()
        can_manage = self.can_manage_all(request)
        if not can_manage:
            user_cust = self.get_user_customer(request)
            if address.customer != user_cust:
                raise exceptions.PermissionDenied(
                    "You can only set your own address as default."
                )

        updated = services.set_default_address(address)
        serializer = self.get_serializer(updated)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "post"], url_path="me")
    def me(self, request):
        """Convenience endpoint to get or create address for the logged-in user."""
        user_cust = self.get_user_customer(request)

        if request.method == "GET":
            addresses = CustomerAddress.objects.filter(
                customer=user_cust, customer__store=request.store
            ).order_by("-is_default", "-created_at")
            serializer = self.get_serializer(addresses, many=True)
            return Response(serializer.data)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        address = services.save_address(serializer, customer=user_cust)
        return Response(self.get_serializer(address).data, status=status.HTTP_201_CREATED)
