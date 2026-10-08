import uuid

from django.db import models

from apps.tenancy.models import Store


class ThemeFile(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        db_column="store_id",
        related_name="theme_files",
    )

    path = models.CharField(max_length=255)
    content = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"themes"."theme_files"'
        verbose_name = "Theme file"
        verbose_name_plural = "Theme files"
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "path"],
                name="unique_theme_file_path_per_store",
            ),
        ]

    def __str__(self):
        return f"{self.store_id}:{self.path}"
