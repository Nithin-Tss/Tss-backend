
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0001_initial"),
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="payment",
            old_name="order_id",
            new_name="order",
        ),
        migrations.AlterField(
            model_name="payment",
            name="order",
            field=models.ForeignKey(
                to="orders.order",
                on_delete=django.db.models.deletion.PROTECT,
                db_column="order_id",
                related_name="payments",
                null=True,
                blank=True,
            ),
        ),
    ]
