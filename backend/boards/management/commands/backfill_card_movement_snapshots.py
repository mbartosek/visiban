"""
Fill CardMovement.board / card_uid / card_title on rows that still lack them (#1469).

Migration ``boards.0080`` does this for every existing row at upgrade time.
Any install that migrates while the previous release is still serving (the
documented Compose steps, and Helm, which migrates in the new pod's init
container) can get a movement written by the old code after that migration; it
has no ``board`` or snapshot yet. Board History still lists such a row through
its card, so nothing is lost from view; this command fills the columns so the
row also survives its card being deleted. Run it once after the rollout
completes. It is idempotent and safe to run at any time — it touches only rows
that have a card but no board, in committed batches.

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
