from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0004_payment_ledger'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.UniqueConstraint(
                condition=models.Q(('provider', 'stripe'), ('provider_payment_id__isnull', False), ('is_deleted', False)),
                fields=('provider', 'provider_payment_id'),
                name='uq_payment_stripe_intent',
            ),
        ),
    ]
