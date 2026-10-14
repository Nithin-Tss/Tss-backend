import uuid

from django.db import models

from apps.tenancy.models import Store


class Theme(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=120, unique=True)
    description = models.TextField(null=True, blank=True)
    category = models.CharField(max_length=50, null=True, blank=True)
    version = models.CharField(max_length=20, default="1.0.0")
    thumbnail_url = models.TextField(null=True, blank=True)
    theme_data = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    is_premium = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"themes"."themes"'
        verbose_name = "Theme"
        verbose_name_plural = "Themes"

    def __str__(self):
        return self.name


class ThemeFile(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    theme = models.ForeignKey(
        Theme,
        on_delete=models.PROTECT,
        db_column="theme_id",
        related_name="files",
    )
    file_path = models.CharField(max_length=500)
    file_name = models.CharField(max_length=255)
    directory = models.CharField(max_length=100)
    file_type = models.CharField(max_length=30)
    content = models.TextField()
    is_editable = models.BooleanField(default=True)
    is_required = models.BooleanField(default=False)
    file_size = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"themes"."theme_files"'
        verbose_name = "Theme File"
        verbose_name_plural = "Theme Files"

    def __str__(self):
        return self.file_path


class StoreTheme(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="theme_assignments",
    )
    theme = models.ForeignKey(
        Theme,
        on_delete=models.PROTECT,
        db_column="theme_id",
        related_name="store_assignments",
    )
    installed_version = models.CharField(max_length=20)
    custom_data = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"themes"."store_themes"'
        verbose_name = "Store Theme"
        verbose_name_plural = "Store Themes"
        constraints = [
            models.UniqueConstraint(
                fields=["store"],
                condition=models.Q(is_active=True),
                name="uq_store_theme_active",
            ),
        ]

    def __str__(self):
        return f"{self.store} - {self.theme}"

class ThemeImage(models.Model):
    """A picture uploaded in the customizer (banner, slide, image block); the bytes live in the database."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        db_column="store_id",
        related_name="theme_images",
    )
    content_type = models.CharField(max_length=50)
    data = models.BinaryField()
    size = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"themes"."theme_images"'
        verbose_name = "Theme Image"
        verbose_name_plural = "Theme Images"

    def __str__(self):
        return f"{self.store_id}/{self.id}"


class BlogPost(models.Model):
    """A post on the store's blog: /blog lists the published ones, /blog/<handle> shows one."""

    DRAFT = "draft"
    PUBLISHED = "published"
    STATUSES = [(DRAFT, "Draft"), (PUBLISHED, "Published")]

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="blog_posts",
    )
    title = models.CharField(max_length=255)
    # Its address on the store: /blog/<handle>
    handle = models.SlugField(max_length=255)
    excerpt = models.TextField(blank=True, default="")
    body = models.TextField(blank=True, default="")
    cover_image = models.TextField(blank=True, default="")
    author = models.CharField(max_length=120, blank=True, default="")
    category = models.CharField(max_length=120, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"themes"."blog_posts"'
        verbose_name = "Blog Post"
        verbose_name_plural = "Blog Posts"
        constraints = [
            models.UniqueConstraint(fields=["store", "handle"], name="uq_blog_post_store_handle"),
        ]
        indexes = [
            models.Index(fields=["store", "status", "published_at"], name="ix_blog_post_store_status"),
        ]

    def __str__(self):
        return self.title
