"""#1661 — NOT NULL columns added by AddField must keep a DB-level default.

Django drops the column default after AddField on PostgreSQL, so during a rolling
upgrade an old pod that INSERTs without naming the new column hits a NOT NULL
violation. ``db_default`` keeps the default in the database. These tests assert
the default is present in the live schema and that an INSERT omitting the
columns, as an old pod's would, succeeds.
"""

from django.db import connection
from django.test import TestCase

from accounts.models import SiteSetting, User
from boards.models import Board

COLUMNS = [
    (User, "notif_stale"),
    (User, "email_notif_card_assigned"),
    (User, "email_notif_mentioned"),
    (User, "email_notif_due_soon"),
    (User, "email_notif_card_moved"),
    (User, "email_notif_comment_added"),
    (SiteSetting, "maintenance_mode"),
    (SiteSetting, "maintenance_message"),
    (Board, "show_wip_at_limit"),
]


def _db_defaults(table):
    with connection.cursor() as cursor:
        desc = connection.introspection.get_table_description(cursor, table)
    return {col.name: col.default for col in desc}


class DbDefaultNotNullColumnsTests(TestCase):
    def test_columns_have_database_default(self):
        cache = {}
        missing = []
        for model, column in COLUMNS:
            table = model._meta.db_table
            defaults = cache.setdefault(table, _db_defaults(table))
            if defaults.get(column) is None:
                missing.append(f"{table}.{column}")
        self.assertEqual(missing, [], "columns with no DB-level default")

    def test_raw_insert_omitting_columns_succeeds(self):
        """Mimic an old pod: INSERT names only the pre-existing columns."""
        table = connection.ops.quote_name(SiteSetting._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {table} (id, registration_mode, uploads_enabled) "
                "VALUES (%s, %s, %s)",
                [99, "open", True],
            )
            cursor.execute(
                f"SELECT maintenance_mode, maintenance_message FROM {table} "
                "WHERE id = %s",
                [99],
            )
            mode, message = cursor.fetchone()
        self.assertFalse(mode)
        self.assertEqual(message, "")
