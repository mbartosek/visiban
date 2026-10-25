# #1880: hash the token of every share link enabled before 0087, so links
# already handed out keep resolving once the share view looks them up by hash.
# Data only, in its own migration ahead of the concurrent unique index (0089).

import hashlib
import uuid

from django.db import migrations

BATCH_SIZE = 500


def hash_share_token(token):
    """Inlined copy of ``Board.hash_share_token`` as of this migration.

    A migration must not import a model helper that may change later.
    ``boards/tests/test_share_token_hash.py`` keeps the two in step.
    """
    canonical = str(token if isinstance(token, uuid.UUID) else uuid.UUID(str(token)))
    return hashlib.sha256(canonical.encode()).hexdigest()


def backfill_share_token_hash(apps, schema_editor):
    """Set ``share_token_hash`` on every board with a token and no hash.

    Idempotent: a board that already has a hash is skipped, so a re-run (or a
    run after a link was enabled by the new code) changes nothing. Each update
    is conditional on the token read, so a link regenerated or cleared
    meanwhile keeps what its writer set.
    """
    Board = apps.get_model("boards", "Board")
    pending = (
        Board.objects.filter(share_token__isnull=False, share_token_hash__isnull=True)
        .values_list("pk", "share_token")
        .order_by("pk")
    )
    for pk, token in pending.iterator(chunk_size=BATCH_SIZE):
        Board.objects.filter(pk=pk, share_token=token, share_token_hash__isnull=True).update(
            share_token_hash=hash_share_token(token),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0087_board_share_token_hash"),
    ]

    operations = [
        # Reverse is a no-op: 0087's reverse drops the column.
        migrations.RunPython(backfill_share_token_hash, migrations.RunPython.noop),
    ]
