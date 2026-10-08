import uuid

from django.db import models

from apps.tenancy.models import Store


class Customer(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="customers",
    )

    first_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    last_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    email = models.EmailField(
        max_length=320,
        null=True,
        blank=True,
    )

    email_subscription_status = models.CharField(
        max_length=20,
    )

    city = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    state = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    country = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"customers"."customers"'
        verbose_name = "Customer"
        verbose_name_plural = "Customers"

    def __str__(self):
        return self.email or str(self.id)


class CustomerAddress(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="addresses",
    )

    address_type = models.CharField(max_length=20)

    first_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    last_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    company = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    address_line_1 = models.CharField(max_length=255)

    address_line_2 = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    city = models.CharField(max_length=100)

    state = models.CharField(max_length=100)

    postal_code = models.CharField(max_length=20)

    country = models.CharField(max_length=100)

    phone = models.CharField(
        max_length=30,
        null=True,
        blank=True,
    )

    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"customers"."customer_addresses"'
        verbose_name = "Customer Address"
        verbose_name_plural = "Customer Addresses"

    def __str__(self):
        return f"{self.customer} - {self.address_type}"