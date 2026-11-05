"""
SQLite backend for running the test suite locally.

The real database is PostgreSQL with one schema per app ("catalog"."products").
SQLite has no schemas, so every "schema"."table" in the SQL is rewritten to a
plain table name: "catalog__products". Nothing else changes.

Used only by config/test_settings.py - never in production.
"""
import re
import sqlite3

from django.db.backends.sqlite3 import base

SCHEMAS = (
    "identity", "tenancy", "customers", "catalog", "inventory", "purchasing",
    "commerce", "payments", "orders", "delivery", "wishlist", "metafields", "discounts",
    "gift_cards", "fulfillment", "themes", "media", "audit", "webhooks", "platform_admin", "analytics", "jobs",
)

_QUALIFIED = re.compile(r'"(%s)"\."([^"]+)"' % "|".join(SCHEMAS))


def _rewrite(sql):
    return _QUALIFIED.sub(r'"\1__\2"', sql) if isinstance(sql, str) else sql


class CursorWrapper(base.SQLiteCursorWrapper):
    def execute(self, query, params=None):
        rewritten = _rewrite(query)
        # Test SQLite without migrations cannot install PostgreSQL's append-only
        # triggers. Mirror their direct row-edit protection while leaving ORM
        # bulk cleanup (used by store purge tests) available.
        immutable_tables = {
            '"inventory__inventory_history"': ("note",),
            '"gift_cards__gift_card_transactions"': ("reason",),
            '"payments__payment_transactions"': ("reason",),
        }
        if isinstance(rewritten, str):
            for table, fields in immutable_tables.items():
                if table not in rewritten:
                    continue
                update = re.match(r"\s*UPDATE\b", rewritten, re.I)
                delete = re.match(r"\s*DELETE\b", rewritten, re.I)
                field_names = "|".join(fields)
                direct_field_update = update and re.search(
                    rf"\bSET\s+(?:\"(?:{field_names})\"|(?:{field_names}))\s*=",
                    rewritten,
                    re.I,
                )
                direct_id_delete = delete and re.search(
                    r"\bWHERE\s+(?:\"id\"|id)\s*=\s*(?:%s|\?)",
                    rewritten,
                    re.I,
                )
                if direct_field_update or direct_id_delete:
                    raise sqlite3.IntegrityError("ledger rows are append-only")
        return super().execute(rewritten, params)

    def executemany(self, query, param_list):
        return super().executemany(_rewrite(query), param_list)


class DatabaseWrapper(base.DatabaseWrapper):
    def create_cursor(self, name=None):
        return self.connection.cursor(factory=CursorWrapper)
