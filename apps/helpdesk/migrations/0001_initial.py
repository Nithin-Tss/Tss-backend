import uuid

import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('tenancy', '0021_store_checkout_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE SCHEMA IF NOT EXISTS "helpdesk";',
            reverse_sql='DROP SCHEMA IF EXISTS "helpdesk" CASCADE;',
        ),
        migrations.CreateModel(
            name='Ticket',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('subject', models.CharField(max_length=150)),
                ('status', models.CharField(choices=[('open', 'Open'), ('pending', 'Pending'), ('closed', 'Closed')], default='open', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('store', models.ForeignKey(db_column='store_id', on_delete=django.db.models.deletion.CASCADE, related_name='support_tickets', to='tenancy.store')),
                ('user', models.ForeignKey(db_column='user_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='support_tickets', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': '"helpdesk"."tickets"',
                'ordering': ['-updated_at', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='TicketMessage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sender_type', models.CharField(choices=[('owner', 'Owner'), ('staff', 'Staff')], max_length=5)),
                ('body', models.TextField()),
                ('attachments', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('sender', models.ForeignKey(db_column='sender_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('ticket', models.ForeignKey(db_column='ticket_id', on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='helpdesk.ticket')),
            ],
            options={
                'db_table': '"helpdesk"."ticket_messages"',
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='ticket',
            index=models.Index(fields=['store', 'user', '-updated_at'], name='idx_ticket_store_user_upd'),
        ),
        migrations.AddIndex(
            model_name='ticket',
            index=models.Index(fields=['status', '-updated_at'], name='idx_ticket_status_upd'),
        ),
        migrations.AddConstraint(
            model_name='ticket',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['open', 'pending', 'closed'])), name='ck_ticket_status'),
        ),
        migrations.AddIndex(
            model_name='ticketmessage',
            index=models.Index(fields=['ticket', 'created_at'], name='idx_ticket_message_ticket'),
        ),
        migrations.AddConstraint(
            model_name='ticketmessage',
            constraint=models.CheckConstraint(condition=models.Q(('sender_type__in', ['owner', 'staff'])), name='ck_ticket_message_sender_type'),
        ),
        migrations.AddConstraint(
            model_name='ticketmessage',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.GreaterThanOrEqual(django.db.models.functions.text.Length('body'), 1),
                    django.db.models.lookups.LessThanOrEqual(django.db.models.functions.text.Length('body'), 8000),
                ),
                name='ck_ticket_message_body_length',
            ),
        ),
    ]
