# #1469 step 1 of 4: the columns that keep a hard-deleted card's movement
# history attached to its board and identifiable.
#
# PostgreSQL locking: every operation here is catalog-only.
# - ``board`` is a new nullable column, so the FK Django adds inline with
#   ADD COLUMN ... REFERENCES has nothing to validate. ``db_index=False``
#   keeps Django from building the implicit FK index here under ACCESS
#   EXCLUSIVE; the composite (board, -moved_at) index is built concurrently
#   in 0081, after the 0080 backfill.
# - ``card_uid`` / ``card_title`` carry ``db_default=""`` (a constant default
#   is metadata-only on PostgreSQL 11+). The DB default also keeps an INSERT
#   from an older release, which does not know these columns, working during
#   a rolling upgrade.
# See docs/development/database-migrations.md.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0077_board_invite_reserved_for_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="cardmovement",
            name="board",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="boards.board",
            ),
        ),
        migrations.AddField(
            model_name="cardmovement",
            name="card_title",
            field=models.CharField(blank=True, db_default="", default="", max_length=500),
        ),
        migrations.AddField(
            model_name="cardmovement",
            name="card_uid",
            field=models.CharField(blank=True, db_default="", default="", max_length=16),
        ),
    ]
