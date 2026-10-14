import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0001_initial"),
        ("themes", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ThemeImage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("content_type", models.CharField(max_length=50)),
                ("data", models.BinaryField()),
                ("size", models.IntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "store",
                    models.ForeignKey(
                        db_column="store_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="theme_images",
                        to="tenancy.store",
                    ),
                ),
            ],
            options={
                "verbose_name": "Theme Image",
                "verbose_name_plural": "Theme Images",
                "db_table": '"themes"."theme_images"',
            },
        ),
    ]
