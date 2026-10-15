"""#1661 — NOT NULL columns added by AddField must keep a DB-level default.

Django drops the column default after AddField on PostgreSQL, so during a rolling
upgrade an old pod that INSERTs without naming the new column hits a NOT NULL
violation. ``db_default`` keeps the default in the database. These tests assert
the default is present in the live schema, which is what the old pod relies on.
"""

from django.db import connection
from django.test import TestCase

COLUMNS = [
    ("users", "notif_stale"),
    ("users", "email_notif_card_assigned"),
    ("users", "email_notif_mentioned"),
    ("users", "email_notif_due_soon"),
    ("users", "email_notif_card_moved"),
    ("users", "email_notif_comment_added"),
    ("site_settings", "maintenance_mode"),
    ("site_settings", "maintenance_message"),
]


def _db_defaults(table):
    with connection.cursor() as cursor:
        desc = connection.introspection.get_table_description(cursor, table)
    return {col.name: col.default for col in desc}


class DbDefaultNotNullColumnsTests(TestCase):
    def test_accounts_columns_have_database_default(self):
        cache = {}
        for table, column in COLUMNS:
            defaults = cache.setdefault(table, _db_defaults(table))
            self.assertIsNotNone(
                defaults.get(column), f"{table}.{column} has no DB-level default"
            )

    def test_board_show_wip_at_limit_has_database_default(self):
        from boards.models import Board

        defaults = _db_defaults(Board._meta.db_table)
        self.assertIsNotNone(
            defaults.get("show_wip_at_limit"),
            "boards.show_wip_at_limit has no DB-level default",
        )
