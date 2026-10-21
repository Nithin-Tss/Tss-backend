from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenancy', '0005_store_settings'),
    ]

    operations = [
        migrations.AddField(model_name='store', name='tax_rate', field=models.DecimalField(decimal_places=2, default=0, max_digits=5)),
        migrations.AddField(model_name='store', name='shipping_flat_rate', field=models.DecimalField(decimal_places=2, default=0, max_digits=10)),
        migrations.AddField(model_name='store', name='free_shipping_threshold', field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
    ]
