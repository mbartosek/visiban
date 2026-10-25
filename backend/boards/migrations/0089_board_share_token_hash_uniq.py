# #1880: unique index on Board.share_token_hash, the column public share links
# are resolved by. Kept alone in its own non-atomic migration and built with
# CREATE UNIQUE INDEX CONCURRENTLY (docs/development/database-migrations.md).
# The constraint is partial (WHERE share_token_hash IS NOT NULL), which Django
# implements as a unique index, not a table constraint, on every backend: no
# ALTER TABLE ... ADD CONSTRAINT step is needed.

from django.db import migrations, models

NAME = "board_share_token_hash_uniq"

CONSTRAINT = models.UniqueConstraint(
    fields=["share_token_hash"], name=NAME,
    condition=models.Q(share_token_hash__isnull=False),
)


def _valid_index_exists(schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s AND i.indrelid = 'boards'::regclass",
            [NAME],
        )
        row = cursor.fetchone()
        return bool(row and row[0])


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        # SQLite (local and worktree test runs): a partial unique constraint is
        # a CREATE UNIQUE INDEX there, built directly; the table is a test
        # database.
        schema_editor.add_constraint(apps.get_model("boards", "Board"), CONSTRAINT)
        return
    if _valid_index_exists(schema_editor):
        return
    # A failed CREATE INDEX CONCURRENTLY leaves an INVALID index behind, which
    # IF NOT EXISTS would then skip on a re-run: drop any such leftover first.
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME};")
    schema_editor.execute(
        f"CREATE UNIQUE INDEX CONCURRENTLY {NAME} ON boards (share_token_hash) "
        "WHERE share_token_hash IS NOT NULL;"
    )


def backward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        schema_editor.remove_constraint(apps.get_model("boards", "Board"), CONSTRAINT)
        return
    schema_editor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME};")


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("boards", "0088_backfill_board_share_token_hash"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            # State-only: tells the ORM the constraint exists so makemigrations
            # stays quiet. The DDL is in the RunPython below.
            state_operations=[
                migrations.AddConstraint(model_name="board", constraint=CONSTRAINT),
            ],
        ),
        migrations.RunPython(forward, backward),
    ]
