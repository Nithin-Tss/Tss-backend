import os
from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.management.commands import createsuperuser
from django.core.management.base import CommandError

from apps.identity.models import normalize_email


class Command(createsuperuser.Command):
    help = (
        "Creates a platform admin. Requires the email and password of an "
        "existing root admin (the first admin can be created freely)."
    )

    def _verify_root_admin(self):
        User = get_user_model()
        if not User.objects.filter(is_owner=True).exists():
            self.stdout.write("No admin exists yet: creating the first (root) admin.")
            return

        email = os.environ.get("ROOT_ADMIN_EMAIL") or input("Root admin email: ")
        password = os.environ.get("ROOT_ADMIN_PASSWORD") or getpass("Root admin password: ")

        root = User.objects.filter(email__iexact=normalize_email(email), is_owner=True).first()
        # Run the hasher even for an unknown email, so timing doesn't reveal valid ones.
        if root is None:
            User().check_password(password)
        if root is None or not root.check_password(password):
            raise CommandError("Invalid root admin credentials. Superuser not created.")

    def handle(self, *args, **options):
        self._verify_root_admin()
        return super().handle(*args, **options)
