from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenancy', '0004_access_hardening'),
    ]

    operations = [
        migrations.AddField(model_name='store', name='contact_email', field=models.CharField(blank=True, default='', max_length=254)),
        migrations.AddField(model_name='store', name='phone', field=models.CharField(blank=True, default='', max_length=30)),
        migrations.AddField(model_name='store', name='address', field=models.CharField(blank=True, default='', max_length=500)),
        migrations.AddField(model_name='store', name='currency', field=models.CharField(default='INR', max_length=3)),
        migrations.AddField(model_name='store', name='backup_region', field=models.CharField(blank=True, default='', max_length=60)),
        migrations.AddField(model_name='store', name='unit_system', field=models.CharField(default='metric', max_length=10)),
        migrations.AddField(model_name='store', name='weight_unit', field=models.CharField(default='kg', max_length=5)),
        migrations.AddField(model_name='store', name='timezone', field=models.CharField(default='Asia/Kolkata', max_length=64)),
        migrations.AddField(model_name='store', name='order_prefix', field=models.CharField(blank=True, default='#', max_length=10)),
        migrations.AddField(model_name='store', name='order_suffix', field=models.CharField(blank=True, default='', max_length=10)),
        migrations.AddField(model_name='store', name='auto_fulfill', field=models.CharField(default='gift_cards', max_length=20)),
        migrations.AddField(model_name='store', name='auto_archive', field=models.BooleanField(default=True)),
    ]
