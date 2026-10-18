# #1704: partial index for BoardInviteLink.reserved_for, built concurrently.
# Kept alone in its own non-atomic migration (docs/development/database-migrations.md).

from django.db import migrations, models

from visiban.db_operations import AddIndexConcurrently


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    atomic = False

    dependencies = [
        ("boards", "0076_boardinvitelink_reserved_for"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="boardinvitelink",
            index=models.Index(
                condition=models.Q(("reserved_for__isnull", False)),
                fields=["reserved_for"],
                name="board_invite_reserved_for_idx",
            ),
        ),
    ]
