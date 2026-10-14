
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0008_collection_conditions_collection_description_and_more"),
        ("tenancy", "0005_store_settings"),
    ]

    operations = [
        migrations.CreateModel(
            name="Brand",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True, null=True)),
                ("logo_url", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "store",
                    models.ForeignKey(
                        db_column="store_id",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="brands",
                        to="tenancy.store",
                    ),
                ),
            ],
            options={
                "verbose_name": "Brand",
                "verbose_name_plural": "Brands",
                "db_table": '"catalog"."brands"',
            },
        ),
        migrations.AddConstraint(
            model_name="brand",
            constraint=models.UniqueConstraint(
                models.functions.Lower(
                    models.functions.Trim("name")
                ),
                "store",
                name="uq_brand_store_name_ci",
            ),
        ),
    ]
