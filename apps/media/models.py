
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.tenancy.models import Store


def media_upload_path(instance, filename):
    """
    Store files in a store-specific directory using a generated filename.
    Example: uploads/<store_uuid>/<generated_uuid>.jpg
    """
    extension = Path(filename).suffix.lower()[:20]
    return f"uploads/{instance.store_id}/{uuid.uuid4().hex}{extension}"


class MediaAsset(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="media_assets",
        db_column="store_id",
    )

    # The picture itself is kept in the database (`data`), because the server's disk is wiped on deploy.
    # It is served publicly at /s/_img/<id>. `file` is only for assets saved on disk.
    data = models.BinaryField(blank=True, null=True)

    file = models.FileField(
        upload_to=media_upload_path,
        max_length=500,
        blank=True,
        null=True,
    )

    original_name = models.CharField(max_length=255)

    mime_type = models.CharField(max_length=100)

    file_size = models.BigIntegerField()

    alt_text = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="uploaded_media_assets",
        db_column="uploaded_by_id",
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"media"."media_assets"'
        ordering = ["-created_at"]

        constraints = [
            models.CheckConstraint(
                condition=Q(file_size__gt=0),
                name="media_asset_size_positive",
            ),
        ]

        indexes = [
            models.Index(
                fields=["store", "created_at"],
                name="media_store_created_idx",
            ),
        ]

    @property
    def url(self):
        """Public address of the picture, relative to the site root."""
        if self.file:
            return self.file.url
        return f"/s/_img/{self.id}"

    def __str__(self):
        return self.original_name
