"""
Housekeeping for checkout stock holds. Run on a schedule (e.g. every few minutes):

    python manage.py expire_reservations

Expired holds already stop counting as held; this marks them released so the table stays small.
"""
from django.core.management.base import BaseCommand

from apps.inventory import services


class Command(BaseCommand):
    help = "Release checkout stock holds that ran out of time."

    def handle(self, *args, **options):
        self.stdout.write(f"Released {services.expire_reservations()} expired stock hold(s).")
