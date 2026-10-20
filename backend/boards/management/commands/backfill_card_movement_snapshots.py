"""
Fill CardMovement.board / card_uid / card_title on rows that still lack them (#1469).

Migration ``boards.0080`` does this for every existing row at upgrade time.
On a multi-replica deployment that rolls its pods, a pod still running the
previous release can write a movement after that migration has run; such a
row has no ``board`` yet, so it is missing from the board-level History until
it is filled. Run this once after the rollout completes. It is idempotent and
safe to run at any time — it touches only rows that have a card but no board,
in committed batches. Single-instance (Docker Compose) installs stop the old
release before migrating and do not need it.

Usage:
    python manage.py backfill_card_movement_snapshots
"""
import importlib

from django.apps import apps
from django.core.management.base import BaseCommand

from boards.models import CardMovement

_backfill = importlib.import_module(
    "boards.migrations.0080_backfill_cardmovement_board_snapshots"
).backfill


class Command(BaseCommand):
    help = "Fill board and card snapshots on movement rows that lack them (#1469)."

    def handle(self, *args, **options):
        pending = CardMovement.objects.filter(board__isnull=True, card__isnull=False)
        before = pending.count()
        # The migration's function takes (apps, schema_editor) and uses only
        # the app registry; the live registry has the same two models.
        _backfill(apps, None)
        self.stdout.write(f"Filled {before - pending.count()} movement row(s).")
