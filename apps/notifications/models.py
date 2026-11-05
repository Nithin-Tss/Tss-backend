import uuid

from django.db import models


class NotificationSettings(models.Model):
    """Settings > Notifications: who the store's emails come from and which events send one. One row per store."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.OneToOneField(
        "tenancy.Store", on_delete=models.CASCADE, db_column="store_id", related_name="notification_settings",
    )

    # Sender: the display name and reply address on customer emails (the sending address itself is the server's)
    sender_name = models.CharField(max_length=100, blank=True, default="")
    reply_to_email = models.EmailField(blank=True, default="")
    email_footer = models.CharField(max_length=500, blank=True, default="")

    # Customer notifications
    customer_order_confirmation = models.BooleanField(default=True)
    customer_order_cancelled = models.BooleanField(default=True)
    customer_order_refunded = models.BooleanField(default=True)
    customer_shipment_delivered = models.BooleanField(default=True)

    # Staff notifications. With no recipients listed, the store's contact email (else its owners) is used.
    staff_new_order = models.BooleanField(default=True)
    staff_order_cancelled = models.BooleanField(default=False)
    staff_recipients = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"notifications"."notification_settings"'

    def __str__(self):
        return f"Notification settings for {self.store_id}"
