from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.audit.models import AuditLog


class Command(BaseCommand):
    help = "Delete audit log entries older than the retention period (AUDIT_LOG_RETENTION_DAYS, default 365)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Keep this many days instead of the setting.")

    def handle(self, *args, **options):
        days = options["days"] if options["days"] is not None else settings.AUDIT_LOG_RETENTION_DAYS
        if days < 1:
            raise CommandError("Keep at least one day of audit log.")
        removed = AuditLog.objects.purge_older_than(days)
        self.stdout.write(f"Removed {removed} audit log entries older than {days} days.")
