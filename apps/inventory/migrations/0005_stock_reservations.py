import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0014_catalogues"),
        ("inventory", "0004_inventory_history_append_only"),
        ("tenancy", "0005_store_settings"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockReservation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("checkout_id", models.UUIDField(db_index=True)),
                ("quantity", models.PositiveIntegerField()),
                ("status", models.CharField(
                    choices=[("active", "Active"), ("converted", "Converted"), ("released", "Released")],
                    default="active", max_length=10)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("store", models.ForeignKey(
                    db_column="store_id", on_delete=django.db.models.deletion.PROTECT,
                    related_name="stock_reservations", to="tenancy.store")),
                ("variant", models.ForeignKey(
                    db_column="variant_id", on_delete=django.db.models.deletion.PROTECT,
                    related_name="stock_reservations", to="catalog.productvariant")),
            ],
            options={
                "verbose_name": "Stock Reservation",
                "verbose_name_plural": "Stock Reservations",
                "db_table": '"inventory"."stock_reservations"',
                "indexes": [models.Index(fields=["variant", "status", "expires_at"], name="idx_stock_resv_variant_live")],
                "constraints": [models.UniqueConstraint(
                    fields=("checkout_id", "variant"), name="uq_stock_reservation_checkout_variant")],
            },
        ),
    ]
