"""
Find and clear card assignees who have board access but are not assignable.

Before the _get_assignable_member_ids() fix, the CardSerializer accepted any
board member as an assignee — including viewers.  This command finds cards
that are still assigned to users with board access who are not assignable (viewers, and
users whose explicit board row overrides a group-inherited role) and either reports them (dry-run)
or clears the assignee field.

Usage:
    # Preview affected cards without making changes:
    python manage.py clear_viewer_assignees --dry-run

    # Clear all stale viewer assignees:
    python manage.py clear_viewer_assignees

    # Restrict to a single board:
    python manage.py clear_viewer_assignees --board 42
"""
from django.core.management.base import BaseCommand

from boards.models import Board, Card
from boards.utils import _get_assignable_member_ids, _get_effective_member_ids


class Command(BaseCommand):
    help = "Clear card assignees who have board access but are not assignable."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report affected cards without modifying anything.",
        )
        parser.add_argument(
            "--board",
            type=int,
            metavar="BOARD_ID",
            help="Restrict the scan to a single board.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        board_id = options.get("board")

        boards_qs = Board.objects.all()
        if board_id:
            boards_qs = boards_qs.filter(pk=board_id)

        total_cleared = 0

        for board in boards_qs.prefetch_related("memberships"):
            # Same sets as the card serializer, so the backfill agrees with the
            # role-resolution ladder (explicit board rows override group
            # inheritance). Only users who still hold board access but are not
            # assignable are candidates: a former member's assignee is left in
            # place on purpose (see notifications_email.py), so it is not touched.
            assignable_ids = _get_assignable_member_ids(board)
            access_only_ids = _get_effective_member_ids(board) - assignable_ids
            viewer_only_ids = set(
                Card.objects.filter(
                    board=board, assignee_id__isnull=False, archived_at__isnull=True
                )
                .values_list("assignee_id", flat=True)
                .distinct()
            ) & access_only_ids

            if not viewer_only_ids:
                continue

            affected = Card.objects.filter(
                board=board,
                assignee_id__in=viewer_only_ids,
                archived_at__isnull=True,
            ).select_related("assignee", "column")

            for card in affected:
                self.stdout.write(
                    f"{'[DRY RUN] ' if dry_run else ''}"
                    f"Board {board.pk} ({board.name!r})  "
                    f"Card {card.pk} ({card.title!r})  "
                    f"assignee={card.assignee.username!r} (not assignable)"
                )

            if not dry_run and affected.exists():
                count = affected.count()
                affected.update(assignee=None)
                total_cleared += count

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes made."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Cleared assignee on {total_cleared} card(s).")
            )
