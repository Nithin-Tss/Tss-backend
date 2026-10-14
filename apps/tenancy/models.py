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
    # Names the owner gave the generic product columns, e.g. {"column1": "Ingredients"}
    field_labels = models.JSONField(default=dict, blank=True)
    # Built-in roles are created once; after that a deleted one stays deleted
    roles_seeded = models.BooleanField(default=False)

    # Settings > General
    contact_email = models.CharField(max_length=254, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    address = models.CharField(max_length=500, blank=True, default="")
    currency = models.CharField(max_length=3, default="INR")
    backup_region = models.CharField(max_length=60, blank=True, default="")
    unit_system = models.CharField(max_length=10, default="metric")
    weight_unit = models.CharField(max_length=5, default="kg")
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    order_prefix = models.CharField(max_length=10, blank=True, default="#")
    order_suffix = models.CharField(max_length=10, blank=True, default="")
    # What happens to an order once it is paid: "all", "gift_cards" or "none"
    auto_fulfill = models.CharField(max_length=20, default="gift_cards")
    auto_archive = models.BooleanField(default=True)

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
        return f"{self.user} -> {self.store}"


class StoreRole(models.Model):
    role_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        db_column="store_id",
        related_name="roles",
    )

    # Stable identifier for built-in roles; NULL for custom roles.
    key = models.CharField(max_length=100, null=True, blank=True)

    kind = models.CharField(max_length=20)
    name = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    permissions = models.JSONField(default=list)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"tenancy"."store_roles"'
        verbose_name = "Store Role"
        verbose_name_plural = "Store Roles"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "name"],
                name="uq_store_role_store_name",
            ),
            models.UniqueConstraint(
                fields=["store", "key"],
                name="uq_store_role_store_key",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=["system", "predefined", "custom"]
                ),
                name="ck_store_role_kind",
            ),
        ]
        indexes = [
            models.Index(
                fields=["store"],
                name="idx_store_role_store",
            ),
        ]

    def __str__(self):
        return f"{self.store} -> {self.name}"


class MembershipRole(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    membership = models.ForeignKey(
        StoreMembership,
        on_delete=models.CASCADE,
        db_column="membership_id",
        related_name="role_assignments",
    )

    role = models.ForeignKey(
        StoreRole,
        on_delete=models.CASCADE,
        db_column="role_id",
        related_name="membership_assignments",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"tenancy"."membership_roles"'
        verbose_name = "Membership Role"
        verbose_name_plural = "Membership Roles"
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "role"],
                name="uq_membership_role_pair",
            ),
        ]
        indexes = [
            models.Index(
                fields=["role"],
                name="idx_membership_role_role",
            ),
        ]

    def __str__(self):
        return f"{self.membership} -> {self.role}"


class Invitation(models.Model):
    """
    Access offered to an email address that has no account yet. It is claimed
    when that address is verified (so nobody can claim access by signing up
    with someone else's email), and lapses after a while.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, db_column="store_id", related_name="invitations",
    )
    email = models.CharField(max_length=254)
    role_ids = models.JSONField(default=list)
    is_owner = models.BooleanField(default=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        db_column="invited_by_id", related_name="+",
    )
    status = models.CharField(max_length=20, default="pending")  # pending / accepted / revoked
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = '"tenancy"."invitations"'
        indexes = [models.Index(fields=["email"], name="idx_invitation_email")]

    def __str__(self):
        return f"{self.email} -> {self.store}"


class AccessLog(models.Model):
    """Who changed roles or access, and when. Rows are never edited."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, db_column="store_id", related_name="access_log",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        db_column="actor_id", related_name="+",
    )
    action = models.CharField(max_length=50)
    subject = models.CharField(max_length=254, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"tenancy"."access_log"'
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["store", "-created_at"], name="idx_access_log_store")]

    def __str__(self):
        return f"{self.action} {self.subject}"
