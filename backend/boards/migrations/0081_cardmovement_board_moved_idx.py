# #1469 step 4 of 4: index for the board-level History (reads by board, newest
# first) and for the board-delete cascade's DELETE ... WHERE board_id IN (...).
# Built after the 0080 backfill so that full-table UPDATE does not churn it.
# Kept alone in its own non-atomic migration (docs/development/database-migrations.md).

from django.db import migrations, models

from visiban.db_operations import AddIndexConcurrently


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("boards", "0080_backfill_cardmovement_board_snapshots"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="cardmovement",
            index=models.Index(fields=["board", "-moved_at"], name="movement_board_moved_idx"),
        ),
    ]
