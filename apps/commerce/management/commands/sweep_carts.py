"""
Housekeeping for shopper carts. Run on a schedule (e.g. hourly):

    python manage.py sweep_carts [--abandon-hours 1] [--expire-days 30]

Flags idle open checkouts as abandoned (for recovery) and expires idle carts.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand

from apps.commerce import services
from apps.tenancy.models import Store


class Command(BaseCommand):
    help = "Mark idle checkouts as abandoned and expire idle carts."

    def add_arguments(self, parser):
        parser.add_argument("--abandon-hours", type=int, default=1)
        parser.add_argument("--expire-days", type=int, default=30)

    def handle(self, *args, **options):
        abandoned = expired = 0
        for store in Store.objects.filter(status="active"):
            abandoned += services.mark_abandoned(store, timedelta(hours=options["abandon_hours"]))
            expired += services.expire_carts(store, timedelta(days=options["expire_days"]))
        self.stdout.write(f"Abandoned {abandoned} checkout(s), expired {expired} cart(s).")
