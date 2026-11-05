from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("delivery", "0002_deliverygateway_metadata_shipment_metadata"),
    ]

    operations = [
        migrations.AddField(model_name="deliverygateway", name="kind",
                            field=models.CharField(choices=[("local", "Local delivery"), ("carrier", "Third-party carrier")], default="carrier", max_length=20)),
        migrations.AddField(model_name="deliverygateway", name="provider",
                            field=models.CharField(default="manual", max_length=30)),
        migrations.AddField(model_name="deliverygateway", name="credentials_enc",
                            field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="deliverygateway", name="webhook_secret_enc",
                            field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="shipment", name="provider_shipment_id",
                            field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="shipment", name="tracking_url",
                            field=models.URLField(blank=True, default="", max_length=500)),
        migrations.AddField(model_name="shipment", name="label_url",
                            field=models.URLField(blank=True, default="", max_length=500)),
    ]
