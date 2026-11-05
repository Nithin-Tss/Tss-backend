import uuid

from django.db import models

from apps.tenancy.models import Store

# What a metafield can be attached to, and the type of every value.
PRODUCT, VARIANT, COLLECTION, BRAND = "product", "variant", "collection", "brand"
OWNER_TYPES = (PRODUCT, VARIANT, COLLECTION, BRAND)
OWNER_CHOICES = [(t, t.capitalize()) for t in OWNER_TYPES]

VALUE_TYPES = (
    "single_line_text", "multi_line_text", "integer", "decimal", "boolean",
    "date", "url", "color", "json", "metaobject_reference",
)
TYPE_CHOICES = [(t, t.replace("_", " ").capitalize()) for t in VALUE_TYPES]


class MetafieldDefinition(models.Model):
    """A custom field a store adds to one kind of resource: `namespace.key` of a type."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, db_column="store_id", related_name="metafield_definitions")
    owner_type = models.CharField(max_length=20, choices=OWNER_CHOICES)
    namespace = models.CharField(max_length=60, default="custom")
    key = models.CharField(max_length=60)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    # Type-specific limits, e.g. {"min": 0, "max": 10}, {"max_length": 80}, {"choices": ["S", "M"]},
    # {"metaobject_type": "author"}.
    validations = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"metafields"."metafield_definitions"'
        verbose_name = "Metafield definition"
        constraints = [
            models.UniqueConstraint(fields=["store", "owner_type", "namespace", "key"], name="uq_metafield_def_key"),
        ]

    def __str__(self):
        return f"{self.owner_type}:{self.namespace}.{self.key}"


class Metafield(models.Model):
    """The value of one definition on one resource (owner_id is that resource's id)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, db_column="store_id", related_name="metafields")
    definition = models.ForeignKey(
        MetafieldDefinition, on_delete=models.CASCADE, db_column="definition_id", related_name="values",
    )
    owner_id = models.UUIDField()
    value = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"metafields"."metafields"'
        verbose_name = "Metafield"
        constraints = [
            models.UniqueConstraint(fields=["definition", "owner_id"], name="uq_metafield_owner"),
        ]
        indexes = [models.Index(fields=["store", "owner_id"], name="ix_metafield_owner")]

    def __str__(self):
        return f"{self.definition} on {self.owner_id}"


class MetaobjectDefinition(models.Model):
    """A custom content type (an "author", a "size chart"): a named list of typed fields."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, db_column="store_id", related_name="metaobject_definitions")
    type = models.SlugField(max_length=60)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # [{"key": "bio", "name": "Bio", "type": "multi_line_text", "required": false, "validations": {}}]
    fields = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"metafields"."metaobject_definitions"'
        verbose_name = "Metaobject definition"
        constraints = [
            models.UniqueConstraint(fields=["store", "type"], name="uq_metaobject_def_type"),
        ]

    def __str__(self):
        return self.type


class Metaobject(models.Model):
    """One entry of a metaobject definition."""

    ACTIVE, DRAFT = "active", "draft"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.PROTECT, db_column="store_id", related_name="metaobjects")
    definition = models.ForeignKey(
        MetaobjectDefinition, on_delete=models.CASCADE, db_column="definition_id", related_name="entries",
    )
    handle = models.SlugField(max_length=120)
    status = models.CharField(max_length=10, default=ACTIVE, choices=[(ACTIVE, "Active"), (DRAFT, "Draft")])
    values = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"metafields"."metaobjects"'
        verbose_name = "Metaobject"
        constraints = [
            models.UniqueConstraint(fields=["definition", "handle"], name="uq_metaobject_handle"),
        ]

    def __str__(self):
        return f"{self.definition.type}/{self.handle}"
