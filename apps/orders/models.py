
import uuid

from django.db import models

from apps.catalog.models import Product, ProductVariant, SalesChannel
from apps.customers.models import Customer
from apps.identity.models import User
from apps.tenancy.models import Store


class Order(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False        
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="orders",
        db_index=False
    )
    order_number = models.CharField(max_length=50)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="orders",
        null=True,
        blank=True,
        db_index=False,      
    )
    sales_channel = models.ForeignKey(
        SalesChannel,
        on_delete=models.PROTECT,
        db_column="sales_channel_id",
        related_name="orders",
        null=True,
        blank=True,
    )
    currency_code = models.CharField(max_length=3)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=30)
    fulfillment_status = models.CharField(max_length=30)
    delivery_status = models.CharField(
        max_length=30,
        null=True,
        blank=True,
    )
    delivery_method = models.CharField(
        max_length=50,
        null=True,
        blank=True,
    )
    # The storefront checkout this order was placed from; null for staff and draft orders.
    checkout = models.OneToOneField(
        "commerce.Checkout",
        on_delete=models.PROTECT,
        db_column="checkout_id",
        related_name="order",
        null=True,
        blank=True,
    )
    # Snapshots taken when the order is placed; null on orders created before these existed.
    shipping_address = models.JSONField(
        null=True,
        blank=True,
    )
    billing_address = models.JSONField(
        null=True,
        blank=True,
    )
    customer_email = models.EmailField(
        max_length=320,
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"orders"."orders"'
        verbose_name = "Order"
        verbose_name_plural = "Orders"
        indexes = [
            models.Index(
                fields=["store", "-created_at"],
                name="ord_store_created_idx",
            ),
            models.Index(
                fields=["customer", "store", "-created_at"],
                name="ord_cust_store_created_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "order_number"],
                name="uq_order_store_number",
            ),

            models.CheckConstraint(
                condition=models.Q(subtotal__gte=0),
                name="ck_ord_subtotal_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_amount__gte=0),
                name="ck_ord_discount_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_amount__lte=models.F("subtotal")),
                name="ck_ord_discount_lte_subtotal",
            ),
            models.CheckConstraint(
                condition=models.Q(tax_amount__gte=0),
                name="ck_ord_tax_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(shipping_amount__gte=0),
                name="ck_ord_shipping_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0),
                name="ck_ord_total_nonneg",
            ),
        ]

    def __str__(self):
        return self.order_number


class OrderItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="order_items",
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="order_items",
        null=True,
        blank=True,
    )
    product_name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, null=True, blank=True)
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField()
    deleted_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = '"orders"."order_items"'
        verbose_name = "Order Item"
        verbose_name_plural = "Order Items"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_order_item_quantity_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name="ck_oi_price_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(discount_amount__gte=0),
                name="ck_oi_discount_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    discount_amount__lte=models.F("unit_price") * models.F("quantity")
                ),
                name="ck_oi_discount_lte_gross",
            ),
            models.CheckConstraint(
                condition=models.Q(tax_amount__gte=0),
                name="ck_oi_tax_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0),
                name="ck_oi_total_nonneg",
            ),
        ]

    def __str__(self):
        return f"{self.order} - {self.product_name}"


class DraftOrder(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="draft_orders",
        db_index=False,
    )
    draft_number = models.CharField(max_length=50)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="draft_orders",
    )
    po_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=30)
    # Estimated from the store's tax rules and the customer's address; the order recalculates it
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    currency_code = models.CharField(max_length=3)

    # Optional link to the normal order created after conversion.
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="source_draft",
        null=True,
        blank=True,
        db_index=False,
    )
    created_by_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        db_column="created_by_user_id",
        related_name="draft_orders_created",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"orders"."draft_orders"'
        verbose_name = "Draft Order"
        verbose_name_plural = "Draft Orders"
        indexes = [
            models.Index(
                fields=["store", "-created_at"],
                name="drf_store_created_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                name="uq_draft_order_order",
            ),
            models.UniqueConstraint(
                fields=["store", "draft_number"],
                name="uq_draft_store_number",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0),
                name="ck_draft_total_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(tax_amount__gte=0),
                name="ck_draft_tax_nonneg",
            ),
        ]

    def __str__(self):
        return self.draft_number


class DraftOrderDetail(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    draft_order = models.ForeignKey(
        DraftOrder,
        on_delete=models.PROTECT,
        db_column="draft_order_id",
        related_name="details",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        db_column="product_id",
        related_name="draft_order_details",
        null=True,
        blank=True,
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.PROTECT,
        db_column="variant_id",
        related_name="draft_order_details",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"orders"."draft_order_details"'
        verbose_name = "Draft Order Detail"
        verbose_name_plural = "Draft Order Details"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_draft_order_detail_quantity_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_price__gte=0),
                name="ck_dod_price_nonneg",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(discount_amount__isnull=True)
                    | models.Q(discount_amount__gte=0)
                ),
                name="ck_dod_discount_nonneg",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(discount_amount__isnull=True)
                    | models.Q(
                        discount_amount__lte=models.F("unit_price") * models.F("quantity")
                    )
                ),
                name="ck_dod_disc_lte_gross",
            ),
            models.CheckConstraint(
                condition=models.Q(total__gte=0),
                name="ck_dod_total_nonneg",
            ),
        ]

    def __str__(self):
        return f"{self.draft_order} - {self.title}"


class OrderIdempotencyKey(models.Model):
    """A client-chosen key for a staff create-order request, so a retry returns the first order."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, db_column="store_id", related_name="+")
    key = models.CharField(max_length=100)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, db_column="order_id", related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"orders"."order_idempotency_keys"'
        constraints = [
            models.UniqueConstraint(fields=["store", "key"], name="uq_order_idem_store_key"),
        ]


class StoreDailyMetric(models.Model):
    """
    One store's sales for one day (in the store's timezone) in one currency, rebuilt from
    orders and refunds by apps.core.reports.aggregate_daily_metrics(). The sales columns are
    derived data: safe to recompute any time. social_clicks is the exception (see below).
    Lives in its own "analytics" schema.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        db_column="store_id",
        related_name="daily_metrics",
        db_index=False,  # the unique (store, metric_date, currency_code) index covers it
    )
    metric_date = models.DateField()
    currency_code = models.CharField(max_length=3)
    total_orders = models.PositiveIntegerField(default=0)
    gross_sales = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_refunds = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_sales = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    items_sold = models.PositiveIntegerField(default=0)
    average_order_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    # Clicks on the storefront's social links that day, per network: {"facebook": 12, "instagram": 5}. Not derived from
    # orders: only record_social_click() writes it (atomically), and aggregate_daily_metrics() leaves it alone.
    social_clicks = models.JSONField(default=dict, db_default={}, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"analytics"."store_daily_metrics"'
        verbose_name = "Store daily metric"
        ordering = ["metric_date", "currency_code"]
        constraints = [
            models.UniqueConstraint(fields=["store", "metric_date", "currency_code"], name="uq_metric_store_date_currency"),
            models.CheckConstraint(condition=models.Q(gross_sales__gte=0), name="ck_metric_gross_nonneg"),
            models.CheckConstraint(condition=models.Q(total_refunds__gte=0), name="ck_metric_refunds_nonneg"),
            models.CheckConstraint(condition=models.Q(average_order_value__gte=0), name="ck_metric_aov_nonneg"),
            models.CheckConstraint(
                condition=models.Q(net_sales=models.F("gross_sales") - models.F("total_refunds")),
                name="ck_metric_net_eq",
            ),
        ]
        indexes = [models.Index(fields=["metric_date"], name="idx_metric_date")]

    def __str__(self):
        return f"{self.store_id} {self.metric_date} {self.currency_code}"


class StorefrontSession(models.Model):
    """
    One visit to a store's storefront. Written by apps.orders.services from the storefront's page
    beacon (POST /s/<store_slug>/_track); read only by the analytics reports. A visit ends after
    30 minutes without activity or at midnight in the store's timezone. No IP address is stored.
    Lives in the "analytics" schema, next to StoreDailyMetric.
    """
    PAGE_TYPES = [(k, k) for k in ("home", "product", "collection", "cart", "checkout", "other")]
    REFERRER_TYPES = [(k, k) for k in ("direct", "search", "social", "email", "paid", "other")]
    DEVICE_TYPES = [(k, k) for k in ("desktop", "mobile", "tablet")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        db_column="store_id",
        related_name="storefront_sessions",
        db_index=False,  # the (store, ...) indexes below cover it
    )
    visitor_id = models.CharField(max_length=64)  # random id kept in the shopper's browser
    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    landing_path = models.CharField(max_length=255, blank=True, default="")  # store-relative, no query string
    landing_page_type = models.CharField(max_length=20, choices=PAGE_TYPES, default="other")
    referrer_host = models.CharField(max_length=255, blank=True, default="")
    referrer_type = models.CharField(max_length=20, choices=REFERRER_TYPES, default="direct")
    utm_source = models.CharField(max_length=100, blank=True, default="")
    utm_medium = models.CharField(max_length=100, blank=True, default="")
    utm_campaign = models.CharField(max_length=100, blank=True, default="")
    device_type = models.CharField(max_length=10, choices=DEVICE_TYPES, default="desktop")
    country = models.CharField(max_length=2, blank=True, default="")  # from CF-IPCountry; blank when unknown
    page_views = models.PositiveIntegerField(default=1)
    added_to_cart = models.BooleanField(default=False)
    reached_checkout = models.BooleanField(default=False)
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        db_column="order_id",
        related_name="storefront_sessions",
        null=True,
        blank=True,
        db_index=False,
    )
    cart_session = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"analytics"."storefront_sessions"'
        verbose_name = "Storefront session"
        ordering = ["started_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(page_views__gte=1), name="ck_sfsession_views_pos"),
            models.CheckConstraint(
                condition=models.Q(last_seen_at__gte=models.F("started_at")), name="ck_sfsession_seen_after_start"),
        ]
        indexes = [
            models.Index(fields=["store", "started_at"], name="idx_sfsession_store_started"),
            models.Index(fields=["store", "last_seen_at"], name="idx_sfsession_store_seen"),
            models.Index(fields=["store", "visitor_id"], name="idx_sfsession_store_visitor"),
        ]

    def __str__(self):
        return f"{self.store_id} {self.visitor_id} {self.started_at:%Y-%m-%d %H:%M}"


class BackgroundJob(models.Model):
    """
    One run of a background job (a Celery task), written by the job helpers in apps.core.tasks.
    A retry of the same Celery task updates its row (retries + 1). Lives in its own "jobs" schema.
    """

    QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
    STATUSES = [(QUEUED, "Queued"), (RUNNING, "Running"), (SUCCEEDED, "Succeeded"), (FAILED, "Failed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Empty for platform-wide jobs (e.g. one run over every store)
    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, db_column="store_id", related_name="background_jobs",
        null=True, blank=True, db_index=False,  # the (store, created_at) index covers it
    )
    job_type = models.CharField(max_length=50)
    task_name = models.CharField(max_length=255)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=QUEUED)
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(null=True, blank=True)
    progress = models.PositiveSmallIntegerField(null=True, blank=True)  # percent, when the job reports it
    error = models.TextField(blank=True, default="")
    retries = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"jobs"."background_jobs"'
        verbose_name = "Background job"
        constraints = [
            models.UniqueConstraint(fields=["celery_task_id"], name="uq_job_celery_task_id"),
            models.CheckConstraint(condition=models.Q(status__in=["queued", "running", "succeeded", "failed"]),
                                   name="ck_job_status_valid"),
            models.CheckConstraint(condition=models.Q(progress__isnull=True) | models.Q(progress__lte=100),
                                   name="ck_job_progress_range"),
            models.CheckConstraint(
                condition=(models.Q(finished_at__isnull=True) | models.Q(started_at__isnull=True)
                           | models.Q(finished_at__gte=models.F("started_at"))),
                name="ck_job_finished_after_started",
            ),
        ]
        indexes = [
            models.Index(fields=["store", "-created_at"], name="idx_job_store_created"),
            models.Index(fields=["job_type", "status", "-created_at"], name="idx_job_type_status"),
        ]

    def __str__(self):
        return f"{self.job_type} {self.status} ({self.id})"
