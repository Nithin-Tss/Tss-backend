import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('tenancy', '0016_store_stripe_keys'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE SCHEMA IF NOT EXISTS "notifications";',
            reverse_sql='DROP SCHEMA IF EXISTS "notifications" CASCADE;',
        ),
        migrations.CreateModel(
            name='NotificationSettings',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sender_name', models.CharField(blank=True, default='', max_length=100)),
                ('reply_to_email', models.EmailField(blank=True, default='', max_length=254)),
                ('email_footer', models.CharField(blank=True, default='', max_length=500)),
                ('customer_order_confirmation', models.BooleanField(default=True)),
                ('customer_order_cancelled', models.BooleanField(default=True)),
                ('customer_order_refunded', models.BooleanField(default=True)),
                ('customer_shipment_delivered', models.BooleanField(default=True)),
                ('staff_new_order', models.BooleanField(default=True)),
                ('staff_order_cancelled', models.BooleanField(default=False)),
                ('staff_recipients', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('store', models.OneToOneField(db_column='store_id', on_delete=django.db.models.deletion.CASCADE, related_name='notification_settings', to='tenancy.store')),
            ],
            options={
                'db_table': '"notifications"."notification_settings"',
            },
        ),
    ]
