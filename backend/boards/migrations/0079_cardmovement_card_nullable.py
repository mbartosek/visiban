# #1469 step 2 of 4: CardMovement.card becomes nullable (SET_NULL), so
# hard-deleting a card keeps its movements.
#
# Why not a plain AlterField: Django 5.2's schema editor drops and re-adds a
# foreign key whenever any db-relevant attribute of the field changes, and
# ``null`` is one. On PostgreSQL that re-ADD validates every card_movements
# row against cards while holding ACCESS EXCLUSIVE on card_movements and
# SHARE ROW EXCLUSIVE on cards — every card write blocks for the whole scan.
# The change actually needed is only DROP NOT NULL, which is catalog-only and
# leaves the existing constraint in place. ``on_delete`` (SET_NULL, plus
# filling board/snapshot on rows that lack them: boards.models.
# set_null_keeping_board) emits no SQL at all; Django's deletion collector
# enforces it.
#
# Other backends (SQLite in development and tests) take the normal
# AlterField path: SQLite rebuilds the table, which is fine there.
# See docs/development/database-migrations.md, "Changing an existing foreign
# key's nullability".

from django.db import migrations, models

import boards.models


class DropNotNullKeepingForeignKey(migrations.AlterField):
    """``AlterField`` whose PostgreSQL DDL is only ``ALTER COLUMN ... DROP NOT NULL``.

    The state change is exactly the inherited ``AlterField``'s. Use it only
    when ``null`` (and Python-only attributes such as ``on_delete``) is the
    sole difference between the old and new field.
    """

    def _table_and_column(self, app_label, state):
        model = state.apps.get_model(app_label, self.model_name)
        return model._meta.db_table, model._meta.get_field(self.name).column

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor != "postgresql":
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        table, column = self._table_and_column(app_label, to_state)
        qn = schema_editor.quote_name
        schema_editor.execute(f"ALTER TABLE {qn(table)} ALTER COLUMN {qn(column)} DROP NOT NULL")

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # Reversing would need every row to have a card again. Rows whose card
        # was hard-deleted are retained audit history: refuse rather than
        # silently delete them, and say how to proceed deliberately.
        table, column = self._table_and_column(app_label, from_state)
        qn = schema_editor.quote_name
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {qn(table)} WHERE {qn(column)} IS NULL LIMIT 1")
            if cursor.fetchone() is not None:
                raise RuntimeError(
                    f"Cannot reverse boards.0079: {table} holds movement history of "
                    f"hard-deleted cards ({column} IS NULL). Restoring NOT NULL would "
                    f"require deleting that history. If that is intended, run "
                    f"DELETE FROM {table} WHERE {column} IS NULL; and migrate again."
                )
        if schema_editor.connection.vendor != "postgresql":
            return super().database_backwards(app_label, schema_editor, from_state, to_state)
        # SET NOT NULL scans the table under ACCESS EXCLUSIVE; acceptable for a
        # rollback, which is an offline operation anyway.
        schema_editor.execute(f"ALTER TABLE {qn(table)} ALTER COLUMN {qn(column)} SET NOT NULL")


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0078_cardmovement_board_snapshots"),
    ]

    operations = [
        DropNotNullKeepingForeignKey(
            model_name="cardmovement",
            name="card",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=boards.models.set_null_keeping_board,
                related_name="movements",
                to="boards.card",
            ),
        ),
    ]
