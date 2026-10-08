import uuid

from django.conf import settings
from django.db import models


class Store(models.Model):
    store_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store_name = models.CharField(max_length=200)
    store_slug = models.CharField(
        max_length=100,
        unique=True,
    )
    status = models.CharField(max_length=30)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"tenancy"."stores"'
        verbose_name = "Store"
        verbose_name_plural = "Stores"

    def __str__(self):
        return self.store_name


class StoreMembership(models.Model):
    membership_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        db_column="user_id",
        related_name="store_memberships",
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="memberships",
    )

    role = models.CharField(max_length=30)
    status = models.CharField(max_length=30)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"tenancy"."store_memberships"'
        verbose_name = "Store Membership"
        verbose_name_plural = "Store Memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "store"],
                name="uq_store_membership_user_store",
            ),
        ]

    def __str__(self):
        return f"{self.user} → {self.store}"