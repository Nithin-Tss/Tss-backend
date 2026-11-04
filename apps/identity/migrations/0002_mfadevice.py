import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('identity', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='MFADevice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('secret', models.CharField(max_length=64)),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('last_used_step', models.BigIntegerField(default=0)),
                ('recovery_codes', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='mfa_device', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'MFA device',
                'verbose_name_plural': 'MFA devices',
                'db_table': '"identity"."mfa_devices"',
            },
        ),
    ]
