from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenancy', '0020_store_customer_accounts'),
    ]

    operations = [
        migrations.AddField(model_name='store', name='checkout_require_login', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='store', name='checkout_full_name', field=models.CharField(default='first_last', max_length=20)),
        migrations.AddField(model_name='store', name='checkout_company', field=models.CharField(default='hidden', max_length=20)),
        migrations.AddField(model_name='store', name='checkout_address2', field=models.CharField(default='optional', max_length=20)),
        migrations.AddField(model_name='store', name='checkout_phone', field=models.CharField(default='optional', max_length=20)),
    ]
