from django.db import migrations

def backfill(apps, schema_editor):
    """Number the existing tickets oldest first, start the counter after them, and rename status pending."""
    Ticket = apps.get_model("helpdesk", "Ticket")
    Counter = apps.get_model("helpdesk", "TicketCounter")
    last = 0
    for last, ticket in enumerate(Ticket.objects.order_by("created_at", "id"), start=1):
        ticket.ticket_number = f"TSS-{last:06d}"
        ticket.save(update_fields=["ticket_number"])
    Counter.objects.update_or_create(pk=1, defaults={"last_number": last})
    Ticket.objects.filter(status="pending").update(status="waiting_on_customer")


def unbackfill(apps, schema_editor):
    """Going back: the new statuses fold into the old three (in_progress -> open, resolved -> closed)."""
    Ticket = apps.get_model("helpdesk", "Ticket")
    Ticket.objects.filter(status="waiting_on_customer").update(status="pending")
    Ticket.objects.filter(status="in_progress").update(status="open")
    Ticket.objects.filter(status="resolved").update(status="closed")


class Migration(migrations.Migration):
    """Data only. Kept apart from the schema changes: PostgreSQL refuses to ALTER a table with pending trigger events."""

    dependencies = [("helpdesk", "0002_support_ticket_fields")]

    operations = [migrations.RunPython(backfill, unbackfill)]
