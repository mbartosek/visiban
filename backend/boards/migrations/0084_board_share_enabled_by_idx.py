# #1865: index for the share-link enabler lookup. Kept alone in its own
# non-atomic migration (docs/development/database-migrations.md).

from django.db import migrations, models

from visiban.db_operations import AddIndexConcurrently


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("boards", "0083_board_share_enabled_by"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="board",
            index=models.Index(
                fields=["share_enabled_by"],
                name="board_share_enabled_by_idx",
                condition=models.Q(share_enabled_by__isnull=False),
            ),
        ),
    ]
