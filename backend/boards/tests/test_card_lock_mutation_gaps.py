"""Assertions for the lock helpers in ``boards/services/cards.py`` (#1931).

SQLite has no row locks, so ``select_for_update(...)`` is a no-op there and the
mutation run left every mutant on its arguments (``no_key=``, ``of=``) alive.
These tests make the call itself observable on any backend: a spy wraps
``QuerySet.select_for_update`` and records the model and keyword arguments of
each call, so the lock mode and the ``of=("self",)`` restriction are pinned
without PostgreSQL. (The behavior those arguments buy, queueing, is pinned on
PostgreSQL by ``test_concurrent_moves.py`` and ``test_concurrent_lock_sites.py``.)

The rest are the guards and retry bounds around those locks: the
``lock_cards_for_cascade`` precondition, its and ``move_card``'s retry loops, the
``lock_card_cell_entry`` branches, ``delete_boards_locked``, the pk sets the
helpers return, and the ``CARD_MUTATION_HOOKS`` failure log.
"""

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from django.db import transaction
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase

from boards.models import Board, Card, Column
from boards.services import cards as svc
from boards.services.errors import ContainerChanged, VersionConflict
from boards.tests.conftest import (
    _make_board,
    _make_card,
    _make_column,
    _make_swimlane,
    _make_user,
)

SELF = ("self",)


@contextmanager
def spy_select_for_update():
    """Record ``(model, kwargs)`` of every ``select_for_update`` call, in order."""
    calls = []
    original = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        calls.append((self.model, kwargs))
        return original(self, *args, **kwargs)

    with patch.object(QuerySet, "select_for_update", spy):
        yield calls


def _render(card, movement=None):
    return {"id": card.pk}


class LockBase(TestCase):
    def setUp(self):
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = _make_user("lock_owner")
        self.board = _make_board(self.owner, enforce_wip_limits=True)
        self.col_a = _make_column(self.board, "A", 0, allow_card_creation=True)
        self.col_b = _make_column(self.board, "B", 1, wip_limit=5)
        self.lane = _make_swimlane(self.board, "L", 0)
        self.card = _make_card(self.col_a, self.lane, title="Subject")


class LockModeTests(LockBase):
    """The mode and scope of each lock statement."""

    def test_enforce_column_limits_locks_the_column_no_key_by_default(self):
        with spy_select_for_update() as calls:
            svc.enforce_column_limits(
                board=self.board, column=self.col_b, card=self.card, role="admin",
            )
        self.assertEqual(calls, [(Column, {"no_key": True})])

    def test_enforce_column_limits_with_lock_false_takes_no_lock(self):
        with spy_select_for_update() as calls:
            svc.enforce_column_limits(
                board=self.board, column=self.col_b, card=self.card, role="admin",
                lock=False,
            )
        self.assertEqual(calls, [])

    def test_create_card_locks_its_column_once_and_does_not_relock_in_the_limit_check(self):
        self.col_a.wip_limit = 5
        self.col_a.save(update_fields=["wip_limit"])

        def save(position):
            return _make_card(self.col_a, self.lane, title="New", position=position)

        with spy_select_for_update() as calls:
            svc.create_card(
                actor=self.owner, board=self.board, column_id=self.col_a.pk,
                swimlane_id=self.lane.pk, save=save, render=_render,
            )
        self.assertEqual(calls, [(Column, {"no_key": True})])

    def test_move_card_locks_cells_and_columns_once_and_does_not_relock_in_the_limit_check(self):
        with spy_select_for_update() as calls:
            svc.move_card(
                actor=self.owner, board=self.board, card_id=self.card.pk,
                target_column_id=self.col_b.pk, target_swimlane_id=self.lane.pk,
                position=0, render=_render,
            )
        self.assertEqual(
            calls,
            [(Card, {"of": SELF}), (Column, {"no_key": True})],
        )

    def test_lock_card_for_write_is_no_key(self):
        with spy_select_for_update() as calls:
            locked = svc.lock_card_for_write(self.card.pk)
        self.assertEqual(locked.pk, self.card.pk)
        self.assertEqual(calls, [(Card, {"no_key": True})])

    def test_lock_card_row_locks_the_card_row_only(self):
        with spy_select_for_update() as calls:
            row = svc.lock_card_row(self.card.pk)
        self.assertEqual(row.pk, self.card.pk)
        self.assertEqual(calls, [(Card, {"of": SELF})])

    def test_lock_move_columns_returns_the_locked_pks(self):
        with spy_select_for_update() as calls:
            locked = svc._lock_move_columns({self.col_a.pk, self.col_b.pk})
        self.assertEqual(locked, frozenset({self.col_a.pk, self.col_b.pk}))
        self.assertEqual(calls, [(Column, {"no_key": True})])

    def test_container_delete_locks_boards_then_cards_then_columns(self):
        with transaction.atomic(), spy_select_for_update() as calls:
            svc.lock_cards_for_cascade(
                Card.objects.filter(board=self.board),
                entry_columns=Column.objects.filter(board=self.board),
                boards=Board.objects.filter(pk=self.board.pk),
            )
        self.assertEqual(
            calls,
            [
                (Board, {"no_key": True, "of": SELF}),
                (Card, {"of": SELF}),
                (Column, {"no_key": True, "of": SELF}),
            ],
        )

    def test_container_delete_with_only_cards_locks_only_the_cards(self):
        """No boards, no entry columns: neither of those two locks is taken."""
        with spy_select_for_update() as calls:
            svc.lock_cards_for_cascade(Card.objects.filter(board=self.board))
        self.assertEqual(calls, [(Card, {"of": SELF})])

    def test_container_delete_with_boards_but_no_entry_columns_locks_boards_then_cards(self):
        with transaction.atomic(), spy_select_for_update() as calls:
            svc.lock_cards_for_cascade(
                Card.objects.filter(board=self.board),
                boards=Board.objects.filter(pk=self.board.pk),
            )
        self.assertEqual(
            calls,
            [(Board, {"no_key": True, "of": SELF}), (Card, {"of": SELF})],
        )


class CascadePkSetTests(LockBase):
    """The helpers hand back plain pks, which the retry compares as sets."""

    def test_lock_cascade_cards_returns_card_pks(self):
        other = _make_card(self.col_a, self.lane, title="Other", position=1)
        locked = svc._lock_cascade_cards(Card.objects.filter(board=self.board))
        self.assertEqual(locked, frozenset({self.card.pk, other.pk}))

    def test_cascade_card_pks_returns_card_pks(self):
        other = _make_card(self.col_a, self.lane, title="Other", position=1)
        self.assertEqual(
            svc._cascade_card_pks(Card.objects.filter(board=self.board)),
            {self.card.pk, other.pk},
        )

    def test_a_container_that_gains_no_card_is_locked_in_one_attempt(self):
        with transaction.atomic(), patch.object(
            svc, "_cascade_card_pks", wraps=svc._cascade_card_pks
        ) as read:
            result = svc.lock_cards_for_cascade(
                Card.objects.filter(board=self.board),
                entry_columns=Column.objects.filter(board=self.board),
            )
        self.assertIsNone(result)
        self.assertEqual(read.call_count, 1)

    def test_a_container_that_gains_a_card_is_retried_and_then_succeeds(self):
        """The first re-read shows an unlocked newcomer; the retry must run again."""
        pks = {self.card.pk}
        with transaction.atomic(), patch.object(
            svc, "_cascade_card_pks", side_effect=[pks | {10**9}, pks]
        ) as read:
            result = svc.lock_cards_for_cascade(
                Card.objects.filter(board=self.board),
                entry_columns=Column.objects.filter(board=self.board),
            )
        self.assertIsNone(result)
        self.assertEqual(read.call_count, 2)

    def test_a_container_that_keeps_gaining_cards_gives_up_after_three_attempts(self):
        self.assertEqual(svc._CASCADE_LOCK_ATTEMPTS, 3)
        pks = {self.card.pk}
        with transaction.atomic(), patch.object(
            svc, "_cascade_card_pks", return_value=pks | {10**9}
        ) as read:
            with self.assertRaises(ContainerChanged):
                svc.lock_cards_for_cascade(
                    Card.objects.filter(board=self.board),
                    entry_columns=Column.objects.filter(board=self.board),
                )
        self.assertEqual(read.call_count, 3)


class CascadePreconditionTests(TransactionTestCase):
    """Outside ``transaction.atomic()``; a TestCase would always be inside one."""

    def setUp(self):
        self.owner = _make_user("pre_owner")
        self.board = _make_board(self.owner)
        self.col = _make_column(self.board, "A", 0)
        self.cards = Card.objects.filter(board=self.board)
        self.columns = Column.objects.filter(board=self.board)
        self.boards = Board.objects.filter(pk=self.board.pk)

    def assert_refused(self, **kwargs):
        with self.assertRaises(RuntimeError) as ctx:
            svc.lock_cards_for_cascade(self.cards, **kwargs)
        self.assertEqual(
            str(ctx.exception),
            "lock_cards_for_cascade must run inside transaction.atomic()",
        )

    def test_entry_columns_alone_need_a_transaction(self):
        self.assert_refused(entry_columns=self.columns)

    def test_boards_alone_need_a_transaction(self):
        self.assert_refused(boards=self.boards)

    def test_both_need_a_transaction(self):
        self.assert_refused(entry_columns=self.columns, boards=self.boards)

    def test_neither_needs_no_transaction(self):
        # Nothing to roll back into, so nothing to refuse. The card lock is
        # stubbed: on PostgreSQL its own select_for_update would raise
        # TransactionManagementError outside a transaction, which is not the
        # guard under test.
        with patch.object(svc, "_lock_cascade_cards", return_value=set()) as lock:
            self.assertIsNone(svc.lock_cards_for_cascade(self.cards))
        lock.assert_called_once_with(self.cards)


class DeleteBoardsLockedTests(TestCase):
    def test_deletes_the_given_boards_with_their_cards_and_nothing_else(self):
        owner = _make_user("del_owner")
        doomed = _make_board(owner, "Doomed")
        doomed_too = _make_board(owner, "Doomed too")
        kept = _make_board(owner, "Kept")
        for board in (doomed, doomed_too, kept):
            col = _make_column(board, "A", 0)
            _make_card(col, _make_swimlane(board, "L", 0), title=f"card of {board.name}")

        with patch.object(
            svc, "lock_cards_for_cascade", wraps=svc.lock_cards_for_cascade
        ) as lock:
            svc.delete_boards_locked(Board.objects.filter(name__startswith="Doomed"))

        self.assertEqual(
            list(Board.objects.values_list("name", flat=True)), ["Kept"]
        )
        self.assertEqual(
            list(Card.objects.values_list("title", flat=True)), ["card of Kept"]
        )
        # Locked as a container delete: the cards, plus the entry columns and board rows.
        lock.assert_called_once()
        self.assertEqual(set(lock.call_args.kwargs), {"entry_columns", "boards"})

    def test_returns_the_queryset_delete_result(self):
        owner = _make_user("del_owner2")
        board = _make_board(owner, "Solo")
        col = _make_column(board, "A", 0)
        _make_card(col, _make_swimlane(board, "L", 0), title="c")
        deleted, per_model = svc.delete_boards_locked(Board.objects.filter(pk=board.pk))
        self.assertGreaterEqual(deleted, 4)  # board, membership, column, swimlane, card
        self.assertEqual(per_model["boards.Board"], 1)
        self.assertEqual(per_model["boards.Card"], 1)


class LockCardCellEntryTests(LockBase):
    def test_locks_the_card_row_first_then_the_non_null_columns(self):
        with patch.object(svc, "lock_card_row", wraps=svc.lock_card_row) as row, \
                spy_select_for_update() as calls:
            locked = svc.lock_card_cell_entry(
                card_id=self.card.pk, column_ids=[self.col_a.pk, None, self.col_b.pk],
            )
        row.assert_called_once_with(self.card.pk)
        self.assertEqual(locked, frozenset({self.col_a.pk, self.col_b.pk}))
        self.assertEqual(calls, [(Card, {"of": SELF}), (Column, {"no_key": True})])

    def test_without_a_card_id_only_the_columns_are_locked(self):
        with patch.object(svc, "lock_card_row") as row:
            locked = svc.lock_card_cell_entry(card_id=None, column_ids=[self.col_a.pk])
        row.assert_not_called()
        self.assertEqual(locked, frozenset({self.col_a.pk}))

    def test_a_deleted_column_is_missing_from_the_result(self):
        gone = self.col_b.pk
        self.col_b.delete()
        locked = svc.lock_card_cell_entry(
            card_id=None, column_ids=[self.col_a.pk, gone],
        )
        self.assertEqual(locked, frozenset({self.col_a.pk}))


class MoveRetryBoundTests(LockBase):
    def _move(self):
        return svc.move_card(
            actor=self.owner, board=self.board, card_id=self.card.pk,
            target_column_id=self.col_b.pk, target_swimlane_id=self.lane.pk,
            position=0, render=_render,
        )

    def test_a_changed_card_is_retried_and_the_retry_result_returned(self):
        changed = svc._CardChangedBeforeLock(7)
        with patch.object(svc, "_move_card_attempt", side_effect=[changed, "moved"]) as attempt:
            self.assertEqual(self._move(), "moved")
        self.assertEqual(attempt.call_count, 2)

    def test_a_card_that_keeps_changing_ends_in_a_version_conflict_after_three_attempts(self):
        self.assertEqual(svc._MOVE_LOCK_ATTEMPTS, 3)
        changed = svc._CardChangedBeforeLock(7)
        with patch.object(svc, "_move_card_attempt", side_effect=changed) as attempt:
            with self.assertRaises(VersionConflict) as ctx:
                self._move()
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(ctx.exception.current_version, 7)


class BoardShareLockTests(LockBase):
    def test_postgresql_takes_the_board_row_for_share(self):
        connection = MagicMock(vendor="postgresql")
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        with patch.object(svc, "connection", connection):
            self.assertTrue(svc.lock_board_for_card_write(self.board.pk))
        cursor.execute.assert_called_once_with(
            'SELECT 1 FROM "boards" WHERE "id" = %s FOR SHARE', [self.board.pk],
        )

    def test_other_backends_only_check_that_the_board_exists(self):
        connection = MagicMock(vendor="sqlite")
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        with patch.object(svc, "connection", connection):
            self.assertFalse(svc.lock_board_for_card_write(self.board.pk))
        cursor.execute.assert_called_once_with(
            'SELECT 1 FROM "boards" WHERE "id" = %s', [self.board.pk],
        )


class HookFailureLogTests(TestCase):
    def test_a_raising_handler_is_logged_by_name_event_ids_and_exception_class(self):
        def exploding_handler(event, card_id, board_id, actor_id):
            raise ValueError("secret payload must not be logged")

        with patch.object(svc.hooks, "CARD_MUTATION_HOOKS", [exploding_handler]):
            with self.assertLogs("boards.services.cards", level="ERROR") as logs:
                svc._run_hooks("card.created", 11, 22, 33)

        (record,) = logs.records
        self.assertEqual(
            record.getMessage(),
            "CARD_MUTATION_HOOKS handler "
            f"{exploding_handler.__qualname__} failed for card.created "
            "(card=11 board=22): ValueError",
        )
        self.assertNotIn("secret payload", record.getMessage())
        self.assertEqual(record.levelno, logging.ERROR)

    def test_a_handler_without_a_qualname_is_logged_by_repr(self):
        class Handler:
            __slots__ = ()

            def __call__(self, *args):
                raise RuntimeError

            def __repr__(self):
                return "<opaque handler>"

        handler = Handler()
        # An instance has no __qualname__ (only its class does).
        self.assertFalse(hasattr(handler, "__qualname__"))
        with patch.object(svc.hooks, "CARD_MUTATION_HOOKS", [handler]):
            with self.assertLogs("boards.services.cards", level="ERROR") as logs:
                svc._run_hooks("card.moved", 1, 2, 3)
        self.assertIn("handler <opaque handler> failed", logs.records[0].getMessage())
