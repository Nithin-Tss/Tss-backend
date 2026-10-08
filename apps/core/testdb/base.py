"""
SQLite backend for running the test suite locally.

The real database is PostgreSQL with one schema per app ("catalog"."products").
SQLite has no schemas, so every "schema"."table" in the SQL is rewritten to a
plain table name: "catalog__products". Nothing else changes.

Used only by config/test_settings.py - never in production.
"""
import re

from django.db.backends.sqlite3 import base

SCHEMAS = (
    "identity", "tenancy", "customers", "catalog", "inventory", "purchasing",
    "commerce", "payments", "orders", "delivery", "wishlist", "discounts",
    "gift_cards", "fulfillment", "themes",
)

_QUALIFIED = re.compile(r'"(%s)"\."([^"]+)"' % "|".join(SCHEMAS))


def _rewrite(sql):
    return _QUALIFIED.sub(r'"\1__\2"', sql) if isinstance(sql, str) else sql


class CursorWrapper(base.SQLiteCursorWrapper):
    def execute(self, query, params=None):
        return super().execute(_rewrite(query), params)

    def executemany(self, query, param_list):
        return super().executemany(_rewrite(query), param_list)


class DatabaseWrapper(base.DatabaseWrapper):
    def create_cursor(self, name=None):
        return self.connection.cursor(factory=CursorWrapper)
