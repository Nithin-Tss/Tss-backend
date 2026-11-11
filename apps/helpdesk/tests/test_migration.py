"""
The 0002 data migration: existing tickets are numbered oldest first, "pending" becomes "waiting_on_customer" and the
counter continues after them. Needs PostgreSQL with the real migrations (config.test_settings_pg).
"""
import unittest
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

# Other apps stay at their newest migration so their historical models match the database
OTHERS = [("identity", "0003_user_is_support_agent"), ("ai_assistant", "0002_conversation_assistant_type")]
BEFORE = [("helpdesk", "0001_initial"), *OTHERS]
AFTER = [("helpdesk", "0004_ticket_rules_and_help_articles"), *OTHERS]


@unittest.skipUnless(connection.vendor == "postgresql", "needs the real migrations: config.test_settings_pg")
class BackfillTests(TransactionTestCase):
    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        # TransactionTestCase does not undo what the migrations' historical models wrote: remove it by hand
        from apps.identity.models import User
        from apps.tenancy.models import Store

        Store.objects.filter(store_slug="old-shop").delete()  # its tickets go with it
        User.objects.filter(email="old@example.com").delete()

    def test_numbers_statuses_and_counter(self):
        old = self.migrate(BEFORE)
        Store, User, Ticket = old.get_model("tenancy", "Store"), old.get_model("identity", "User"), old.get_model("helpdesk", "Ticket")
        store = Store.objects.create(store_name="Old", store_slug="old-shop", status="active")
        user = User.objects.create(email="old@example.com", first_name="O", last_name="U", password="x")
        ids = []
        for status in ("pending", "open", "closed"):
            ids.append(Ticket.objects.create(store=store, user=user, subject=status, status=status).id)

        new = self.migrate(AFTER)
        Ticket, Counter = new.get_model("helpdesk", "Ticket"), new.get_model("helpdesk", "TicketCounter")
        rows = {t.subject: t for t in Ticket.objects.all()}
        self.assertEqual(rows["pending"].status, "waiting_on_customer")
        self.assertEqual(sorted(t.ticket_number for t in rows.values()), ["TSS-000001", "TSS-000002", "TSS-000003"])
        self.assertEqual(rows["pending"].priority, "normal")
        self.assertEqual(rows["pending"].source, "help_center")
        self.assertEqual(Counter.objects.get(pk=1).last_number, 3)

        # and going back folds the statuses into the old three
        back = self.migrate(BEFORE)
        self.assertEqual(back.get_model("helpdesk", "Ticket").objects.get(subject="pending").status, "pending")
