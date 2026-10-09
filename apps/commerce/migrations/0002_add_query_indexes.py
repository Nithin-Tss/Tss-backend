
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="cart",
            index=models.Index(
                fields=["store", "is_deleted", "-updated_at"],
                name="cart_store_del_upd_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="checkout",
            index=models.Index(
                fields=["store", "is_deleted", "-updated_at"],
                name="chk_store_del_upd_idx",
            ),
        ),
    ]
