from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0017_tax_rules'),
    ]

    operations = [
        migrations.AddField(
            model_name='productvariant',
            name='compare_at_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
    ]
