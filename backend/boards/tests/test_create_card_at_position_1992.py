"""``create_card_at_position``: create plus reorder in one transaction (#1992).

The MCP ``create_card`` tool with ``position`` used to commit the create and
then reorder in a second transaction, so a failed reorder left the card behind
an error with no card id and a retry duplicated it. The tool-level behavior
(nothing written, no frame, safe retry) is pinned in
``mcp_server/tests/test_mcp_crud.py``; this file pins the service's lock order,
which is what keeps the single transaction from deadlocking against a
concurrent move, and its membership re-check.
"""
import threading
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from boards.models import Board, BoardEvent, BoardMembership, Card, CardMovement, Column, Swimlane
from boards.services import cards as card_services
from boards.services.errors import VersionConflict
from boards.tests.lock_wait import assert_blocked_on_lock

PATCH_BROADCAST = "boards.broadcast.broadcast_board_event"
WAIT = 10


class _Fixture:
    def _setup_board(self):
        self.user = User.objects.create_user(username="placer-1992", password="pass")
        self.board = Board.objects.create(name="Placement 1992", owner=self.user)
        BoardMembership.objects.create(
            board=self.board, user=self.user, role=BoardMembership.Role.ADMIN,
        )
        self.column = Column.objects.create(
            board=self.board, name="Backlog", position=0, allow_card_creation=True,
        )
        self.swimlane = Swimlane.objects.create(board=self.board, name="General", position=0)
        self.sibling = Card.objects.create(
            board=self.board, column=self.column, swimlane=self.swimlane,
            title="Sibling", created_by=self.user, position=0,
        )

    def _place(self, title="Placed", position=0):
        def save(position):
            return Card.objects.create(
                board=self.board, column=self.column, swimlane=self.swimlane,
                title=title, created_by=self.user, position=position,
            )

        return card_services.create_card_at_position(
            actor=self.user, board=self.board,
            column_id=self.column.pk, swimlane_id=self.swimlane.pk, position=position,
            save=save, render=lambda card: {"id": card.pk},
            render_moved=lambda card, movement: {"card": {"id": card.pk}},
        )


class CellMembershipRecheckTests(_Fixture, TestCase):
    """The re-check after the column lock (the #1567 shape, for a create)."""

    def setUp(self):
        self._setup_board()

    def _newcomer_on_column_lock(self, times):
        """Patch the column lock so a card enters the cell just before it is
        granted, as a concurrent create or move committing during the wait
        would, on the first ``times`` calls."""
        real = card_services._lock_move_columns
        calls = {"n": 0}

        def entering(column_ids):
            calls["n"] += 1
            if calls["n"] <= times:
                Card.objects.create(
                    board=self.board, column=self.column, swimlane=self.swimlane,
                    title=f"Newcomer {calls['n']}", created_by=self.user,
                    position=100 + calls["n"],
                )
            return real(column_ids)

        return patch.object(card_services, "_lock_move_columns", side_effect=entering), calls

    def test_newcomer_once_is_retried_and_the_card_is_placed(self):
        patcher, _calls = self._newcomer_on_column_lock(times=1)
        with patcher, patch(PATCH_BROADCAST):
            created, moved = self._place()
        self.assertIsNotNone(moved)
        created.card.refresh_from_db()
        self.assertEqual(created.card.position, 0)
        # The first attempt rolled back to its savepoint, newcomer included.
        self.assertFalse(Card.objects.filter(title="Newcomer 1").exists())

    def test_newcomer_on_every_attempt_is_a_conflict_with_nothing_written(self):
        cards_before = Card.objects.count()
        events_before = BoardEvent.objects.count()
        patcher, calls = self._newcomer_on_column_lock(times=99)
        with patcher, patch(PATCH_BROADCAST), self.assertRaises(VersionConflict) as ctx:
            self._place()
        self.assertEqual(calls["n"], card_services._MOVE_LOCK_ATTEMPTS)
        # No card exists, so there is no version to report.
        self.assertIsNone(ctx.exception.body()["current_version"])
        self.assertEqual(ctx.exception.body()["code"], "version_conflict")
        self.assertEqual(Card.objects.count(), cards_before)
        self.assertEqual(BoardEvent.objects.count(), events_before)
        self.assertFalse(CardMovement.objects.filter(card__title="Placed").exists())

    def test_failed_reorder_rolls_back_the_create(self):
        with patch.object(card_services, "move_card", side_effect=VersionConflict(2)), \
                patch(PATCH_BROADCAST), self.assertRaises(VersionConflict):
            self._place()
        self.assertFalse(Card.objects.filter(title="Placed").exists())
        self.assertFalse(BoardEvent.objects.filter(board_id=self.board.id).exists())

    def test_position_equal_to_the_append_position_skips_the_move(self):
        with patch(PATCH_BROADCAST), patch.object(card_services, "move_card") as move:
            created, moved = self._place(position=1)
        move.assert_not_called()
        self.assertIsNone(moved)
        self.assertEqual(created.card.position, 1)


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class PlacementLockOrderTests(_Fixture, TransactionTestCase):
    def setUp(self):
        self._setup_board()

    def test_locks_board_then_cell_cards_then_column(self):
        """Pins the statements: the card-entry order (board ``FOR SHARE`` ->
        cell cards, one pk-ordered statement -> column ``FOR NO KEY UPDATE``),
        taken before ``create_card`` or ``move_card`` runs."""
        with CaptureQueriesContext(connection) as ctx, patch(PATCH_BROADCAST):
            self._place()
        locking = [q["sql"] for q in ctx.captured_queries if " FOR " in q["sql"]]
        self.assertIn('FROM "boards"', locking[0])
        self.assertIn("FOR SHARE", locking[0])
        self.assertIn('FROM "cards"', locking[1])
        # Only the pk is selected, so Django may render the pk order as "1".
        self.assertRegex(locking[1], r'ORDER BY ("cards"\."id"|1) ASC')
        self.assertIn('FOR UPDATE OF "cards"', locking[1])
        self.assertIn('FROM "columns"', locking[2])
        self.assertIn("FOR NO KEY UPDATE", locking[2])

    def test_waits_on_cell_cards_without_holding_the_column(self):
        """A move that holds the cell's cards and then wants the column (its
        own lock order) must get the column while this create waits.

        Under a bare outer ``atomic()`` around create then move, the create
        would hold the column row and then wait on the cards: the move's
        column lock would wait on it, a deadlock. ``NOWAIT`` makes that
        observable at once instead of after ``deadlock_timeout``.
        """
        holding = threading.Event()
        release = threading.Event()
        errors = []
        results = {}

        def mover():
            try:
                with transaction.atomic():
                    list(
                        Card.objects.filter(pk=self.sibling.pk)
                        .select_for_update(of=("self",)).values_list("pk", flat=True)
                    )
                    holding.set()
                    if not release.wait(WAIT):
                        raise RuntimeError("never released")
                    # The move's second step. Raises if the create holds it.
                    list(
                        Column.objects.filter(pk=self.column.pk)
                        .select_for_update(no_key=True, nowait=True)
                        .values_list("pk", flat=True)
                    )
            except BaseException as exc:  # noqa: BLE001 - surfaced after join
                errors.append(exc)
            finally:
                connections.close_all()

        def creator():
            try:
                if not holding.wait(WAIT):
                    raise RuntimeError("mover never took the card lock")
                with patch(PATCH_BROADCAST):
                    results["created"], results["moved"] = self._place()
            except BaseException as exc:  # noqa: BLE001 - surfaced after join
                errors.append(exc)
            finally:
                connections.close_all()

        t_mover = threading.Thread(target=mover, name="mover-1992")
        t_creator = threading.Thread(target=creator, name="creator-1992")
        t_mover.start()
        t_creator.start()
        try:
            assert_blocked_on_lock(
                self, t_creator, "the create should queue on the cell's card rows",
            )
        finally:
            release.set()
            t_mover.join(WAIT * 3)
            t_creator.join(WAIT * 3)
        self.assertFalse(t_mover.is_alive() or t_creator.is_alive(), "a thread hung")
        self.assertEqual(errors, [])
        card = Card.objects.get(pk=results["created"].card.pk)
        self.assertEqual(card.position, 0)
        self.sibling.refresh_from_db()
        self.assertEqual(self.sibling.position, 1)
