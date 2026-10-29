import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class FeatureFlag(models.Model):
    """
    A switch the platform admin controls. `enabled` turns the feature on for
    stores in general, `rollout_percent` limits that to a stable share of stores,
    and a FeatureFlagOverride forces it on or off for one store.
    Flags that code asks about but that have no row use their default in flags.REGISTRY.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=False)
    rollout_percent = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"platform_admin"."feature_flags"'
        ordering = ["key"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rollout_percent__gte=0) & models.Q(rollout_percent__lte=100),
                name="ck_feature_flag_rollout_range",
            ),
        ]

    def __str__(self):
        return self.key


class FeatureFlagOverride(models.Model):
    """One store's own answer for a flag, whatever the flag says for everyone else."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name="overrides")
    store = models.ForeignKey(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="flag_overrides",
    )
    enabled = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"platform_admin"."feature_flag_overrides"'
        constraints = [
            models.UniqueConstraint(fields=["flag", "store"], name="uq_flag_override_flag_store"),
        ]

    def __str__(self):
        return f"{self.flag_id}@{self.store_id}={self.enabled}"


class ApiKey(models.Model):
    """
    A credential a store's own software uses instead of a person's sign-in.
    Only a hash of the secret is stored; the full key is shown once, when it is made.
    A key acts inside one store with just the permissions in `scopes`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="api_keys",
    )
    name = models.CharField(max_length=100)
    # First characters of the key, safe to show and used to find the row
    prefix = models.CharField(max_length=16, unique=True)
    secret_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    # The account that made the key; requests with the key are recorded as theirs
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        db_column="created_by_id", related_name="+",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"platform_admin"."api_keys"'
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["store", "-created_at"], name="idx_api_key_store")]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @property
    def status(self):
        if self.revoked_at:
            return "revoked"
        if self.expires_at and self.expires_at <= timezone.now():
            return "expired"
        return "active"
