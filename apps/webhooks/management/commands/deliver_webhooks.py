import time

from django.core.management.base import BaseCommand

from apps.webhooks import services


class Command(BaseCommand):
    help = (
        "Send queued webhook deliveries (and retries that are due). Run it as a worker with --loop, "
        "or from cron every minute without."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running instead of stopping when the queue is empty.")
        parser.add_argument("--interval", type=float, default=5, help="Seconds to wait between checks with --loop.")
        parser.add_argument("--batch", type=int, default=50, help="Deliveries to pick up at a time.")

    def handle(self, *args, **options):
        while True:
            sent = services.deliver_due(options["batch"])
            if sent:
                self.stdout.write(f"Attempted {sent} webhook deliveries.")
            if not options["loop"]:
                break
            if not sent:
                time.sleep(options["interval"])
