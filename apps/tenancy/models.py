import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower, Trim


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
    # Hostname this store's storefront is served on, e.g. "my-shop.example.com"
    domain = models.CharField(max_length=253, unique=True, null=True, blank=True)
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

    # Checkout pricing: tax is a percentage of (subtotal - discount); shipping is
    # a flat fee, waived once the discounted subtotal reaches free_shipping_threshold.
    # ShippingRate rows (by destination country and cart weight) override the flat fee.
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # Tax rules: TaxRate rows (country / region) override tax_rate. With prices_include_tax
    # the listed prices already contain tax, so tax is reported but not added to the total.
    prices_include_tax = models.BooleanField(default=False)
    tax_shipping = models.BooleanField(default=False)
    shipping_flat_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    free_shipping_threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Stripe Connect (Standard account the store connected to take card payments)
    stripe_account_id = models.CharField(max_length=64, blank=True, default="")
    stripe_charges_enabled = models.BooleanField(default=False)
    stripe_details_submitted = models.BooleanField(default=False)

    # Email of the account that created the store (blank for stores made before this was kept)
    created_by = models.EmailField(blank=True, default="", editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"tenancy"."stores"'
        verbose_name = "Store"
        verbose_name_plural = "Stores"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tax_rate__gte=0) & models.Q(tax_rate__lte=100),
                name="ck_store_tax_rate_range",
            ),
            models.CheckConstraint(
                condition=models.Q(shipping_flat_rate__gte=0),
                name="ck_store_shipping_flat_rate_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(free_shipping_threshold__isnull=True) | models.Q(free_shipping_threshold__gte=0),
                name="ck_store_free_shipping_threshold_nonneg",
            ),
        ]

    def __str__(self):
        return self.store_name


class ShippingZone(models.Model):
    """
    A named group of destinations ("South India", "Europe") that shipping rates
    can be set for, instead of one country per rate.
    """

    zone_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="shipping_zones")
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"tenancy"."shipping_zones"'
        verbose_name = "Shipping zone"
        verbose_name_plural = "Shipping zones"
        constraints = [
            models.UniqueConstraint(
                "store", Lower(Trim("name")),
                name="uq_shipzone_store_name_ci",
                violation_error_message="This store already has a shipping zone with that name.",
            ),
        ]

    def __str__(self):
        return self.name


class ShippingZoneLocation(models.Model):
    """One destination of a zone: a whole country, or one state/region of it."""

    location_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store, on_delete=models.CASCADE, db_column="store_id", related_name="shipping_zone_locations",
    )
    zone = models.ForeignKey(ShippingZone, on_delete=models.CASCADE, db_column="zone_id", related_name="locations")
    country = models.CharField(max_length=100)
    region = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        db_table = '"tenancy"."shipping_zone_locations"'
        verbose_name = "Shipping zone location"
        verbose_name_plural = "Shipping zone locations"
        constraints = [
            models.CheckConstraint(condition=~models.Q(country=""), name="shipzone_loc_country_set"),
            # A destination belongs to at most one zone of the store, matched trimmed and in any case
            models.UniqueConstraint(
                "store", Lower(Trim("country")), Lower(Trim("region")),
                name="uq_shipzone_loc_store_dest_ci",
                violation_error_message="That destination is already in one of this store's shipping zones.",
            ),
        ]

    def __str__(self):
        return f"{self.region + ', ' if self.region else ''}{self.country}"


class ShippingRate(models.Model):
    """
    A shipping fee for carts within a weight band, to one zone, one country or
    (neither set) anywhere. Weights are in the store's weight_unit; the band is
    min_weight <= cart weight < max_weight, with no upper limit when max_weight is blank.
    """

    rate_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="shipping_rates")
    # A zone's rates go with it
    zone = models.ForeignKey(
        ShippingZone, on_delete=models.CASCADE, db_column="zone_id", related_name="rates", null=True, blank=True,
    )
    name = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="")
    min_weight = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    max_weight = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"tenancy"."shipping_rates"'
        verbose_name = "Shipping rate"
        verbose_name_plural = "Shipping rates"
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name="shiprate_price_nonneg"),
            models.CheckConstraint(condition=models.Q(min_weight__gte=0), name="shiprate_minw_nonneg"),
            models.CheckConstraint(
                condition=models.Q(max_weight__isnull=True) | models.Q(max_weight__gt=models.F("min_weight")),
                name="shiprate_max_gt_min",
            ),
            models.CheckConstraint(
                condition=models.Q(zone__isnull=True) | models.Q(country=""),
                name="shiprate_zone_or_country",
            ),
        ]

    def __str__(self):
        return self.name or f"{self.country or 'Anywhere'} {self.min_weight}+"


class TaxRate(models.Model):
    """
    A tax percentage for one destination: a country (blank country = the whole
    world) and optionally one state or region of it. The most specific rule
    for the shopper's address wins; Store.tax_rate applies when none fits.
    """

    rate_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="tax_rates")
    name = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="")
    region = models.CharField(max_length=100, blank=True, default="")
    rate = models.DecimalField(max_digits=5, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"tenancy"."tax_rates"'
        verbose_name = "Tax rate"
        verbose_name_plural = "Tax rates"
        constraints = [
            models.CheckConstraint(condition=models.Q(rate__gte=0) & models.Q(rate__lte=100), name="taxrate_rate_range"),
            # One rule per destination, matched the way the tax engine matches it (trimmed, any case)
            models.UniqueConstraint(
                "store", Lower(Trim("country")), Lower(Trim("region")),
                name="uq_taxrate_store_destination_ci",
                violation_error_message="This store already has a tax rate for that destination.",
            ),
        ]

    def __str__(self):
        return self.name or f"{self.region + ', ' if self.region else ''}{self.country or 'Anywhere'} {self.rate}%"


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
