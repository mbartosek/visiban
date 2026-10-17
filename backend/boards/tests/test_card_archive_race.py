"""Archive and unarchive racing a move, or themselves, on the same card (#1605).

``archive_card`` and ``unarchive_card`` read the card, check ``archived_at``,
then lock the row (#1587). Before the fix they kept deciding from that
pre-lock read, so a write committing in the window between the read and the
lock was missed:

* a move to column B landing first left the ARCHIVED ``CardMovement`` row
  recording A -> A while the card sat in B — a wrong audit trail for the
  cycle-time reports that read it;
* an archive landing first was overwritten with a later ``archived_at`` and
  produced a second ARCHIVED row, broadcast and hook;
* a restore landing first produced a second UNARCHIVED row the same way.

The fix decides from the row ``lock_card_for_write`` returns (copied onto the
caller's instance with ``_adopt_locked_state``, as ``update_card`` does since
#1598): already archived (or already restored) under the lock is the existing
no-op — 200 with the card, nothing written or broadcast — and the movement row
names the card's cell as it is under the lock.

``CardArchiveStaleSnapshotTests`` runs on any backend: the competing write is
applied on the request's own connection just before the lock. The PostgreSQL
class uses real concurrent requests, the archive/unarchive paused after its
read and before its lock while the other request runs to COMMIT. To see the
bug, make ``_adopt_locked_state`` a no-op inside the two services.
"""

import datetime
import threading
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from boards import broadcast as _broadcast
from boards import hooks
from boards.models import (
    Board,
    BoardMembership,
    Card,
    CardMovement,
    CardRelation,
    Column,
    Swimlane,
)
from boards.services import cards as card_services

SERVICES = "boards.services.cards"
HOLD_TIMEOUT = 10.0
ARCHIVED = CardMovement.MovementType.ARCHIVED
UNARCHIVED = CardMovement.MovementType.UNARCHIVED


def _fixture(test, username):
    test.user = User.objects.create_user(username=username, password="pass")
    test.board = Board.objects.create(name="B", owner=test.user)
    BoardMembership.objects.create(
        board=test.board, user=test.user, role=BoardMembership.Role.ADMIN,
    )
    test.col_a = Column.objects.create(board=test.board, name="A", position=0)
    test.col_b = Column.objects.create(board=test.board, name="B", position=1)
    test.swim = Swimlane.objects.create(board=test.board, name="G", position=0)
    test.card = Card.objects.create(
        board=test.board, column=test.col_a, swimlane=test.swim,
        title="C", created_by=test.user, position=0,
    )
    test.base = f"/api/v1/boards/{test.board.pk}/cards/{test.card.pk}"
    # Count hook fan-out: CARD_MUTATION_HOOKS is appended to in place (#820).
    test.hook_events = []

    def handler(event, card_id, board_id, actor_id):
        test.hook_events.append(event)

    hooks.CARD_MUTATION_HOOKS.append(handler)
    test.addCleanup(hooks.CARD_MUTATION_HOOKS.remove, handler)


def _movements(card, movement_type):
    return list(CardMovement.objects.filter(card=card, movement_type=movement_type))


class CardArchiveStaleSnapshotTests(APITestCase):
    """The competing write lands just before the service's lock, on its
    connection — the "committed first" outcome, on any backend."""

    def setUp(self):
        _fixture(self, "racer1605")
        self.client.force_authenticate(self.user)
        patcher = patch("boards.broadcast.broadcast_board_event")
        self.broadcast = patcher.start()
        self.addCleanup(patcher.stop)

    def _before_lock(self, mutate):
        real = card_services.lock_card_for_write

        def racing(card_id):
            mutate()
            return real(card_id)

        return patch(f"{SERVICES}.lock_card_for_write", side_effect=racing)

    def _post(self, url):
        # APITestCase wraps each test in a transaction that never commits, so
        # the deferred broadcasts and hooks are run explicitly here.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url)

    def _events(self, event_type):
        return [c for c in self.broadcast.call_args_list if c.args[1] == event_type]

    def test_archive_after_move_records_the_new_cell(self):
        with self._before_lock(
            lambda: Card.objects.filter(pk=self.card.pk).update(column=self.col_b)
        ):
            resp = self._post(f"{self.base}/archive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        [row] = _movements(self.card, ARCHIVED)
        self.assertEqual(
            (row.from_column_id, row.to_column_id, row.from_column_name, row.to_column_name),
            (self.col_b.pk, self.col_b.pk, "B", "B"),
        )

    def test_archive_of_card_archived_before_lock_is_a_noop(self):
        stamp = timezone.now() - datetime.timedelta(minutes=5)
        with self._before_lock(
            lambda: Card.objects.filter(pk=self.card.pk).update(archived_at=stamp)
        ):
            resp = self._post(f"{self.base}/archive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Card.objects.get(pk=self.card.pk).archived_at, stamp)
        self.assertEqual(_movements(self.card, ARCHIVED), [])
        self.assertEqual(self._events(_broadcast.EVT_CARD_ARCHIVED), [])
        self.assertEqual(self.hook_events, [])

    def test_unarchive_of_card_restored_before_lock_is_a_noop(self):
        Card.objects.filter(pk=self.card.pk).update(archived_at=timezone.now())
        with self._before_lock(
            lambda: Card.objects.filter(pk=self.card.pk).update(archived_at=None)
        ):
            resp = self._post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIsNone(resp.data["archived_at"])
        self.assertEqual(_movements(self.card, UNARCHIVED), [])
        self.assertEqual(self._events(_broadcast.EVT_CARD_UNARCHIVED), [])
        self.assertEqual(self.hook_events, [])

    def test_unarchive_after_move_records_the_new_cell(self):
        Card.objects.filter(pk=self.card.pk).update(archived_at=timezone.now())
        with self._before_lock(
            lambda: Card.objects.filter(pk=self.card.pk).update(column=self.col_b)
        ):
            resp = self._post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["column"], self.col_b.pk)
        [row] = _movements(self.card, UNARCHIVED)
        self.assertEqual((row.from_column_id, row.to_column_id), (self.col_b.pk, self.col_b.pk))

    # -- blocked peers are read under the lock ---------------------------------

    def _peer_with_new_blocker(self):
        """A card that ``self.card`` starts blocking just before the lock."""
        peer = Card.objects.create(
            board=self.board, column=self.col_a, swimlane=self.swim,
            title="P", created_by=self.user, position=1,
        )

        def block():
            CardRelation.objects.create(
                from_card=self.card, to_card=peer,
                relation_type=CardRelation.Type.BLOCKS, created_by=self.user,
            )

        return peer, block

    def _updated_card_ids(self):
        return [
            c.args[2].get("id")
            for c in self._events(_broadcast.EVT_CARD_UPDATED)
        ]

    def test_archive_broadcasts_peer_blocked_before_lock(self):
        peer, block = self._peer_with_new_blocker()
        with self._before_lock(block):
            resp = self._post(f"{self.base}/archive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._updated_card_ids(), [peer.pk])

    def test_unarchive_broadcasts_peer_blocked_before_lock(self):
        Card.objects.filter(pk=self.card.pk).update(archived_at=timezone.now())
        peer, block = self._peer_with_new_blocker()
        with self._before_lock(block):
            resp = self._post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._updated_card_ids(), [peer.pk])

    # -- restore checks the limit of the cell the card is in under the lock ---

    def _archived_card_moved_into_limited_column(self, *, room):
        """Archived in A (no limit); moved into B (WIP 1) just before the lock.
        B holds one active card, or none when ``room``."""
        self.board.enforce_wip_limits = True
        self.board.save(update_fields=["enforce_wip_limits"])
        self.col_b.wip_limit = 1
        self.col_b.save(update_fields=["wip_limit"])
        if not room:
            Card.objects.create(
                board=self.board, column=self.col_b, swimlane=self.swim,
                title="filler", created_by=self.user, position=0,
            )
        Card.objects.filter(pk=self.card.pk).update(archived_at=timezone.now())
        return self._before_lock(
            lambda: Card.objects.filter(pk=self.card.pk).update(
                column=self.col_b, position=1,
            )
        )

    def test_restore_into_full_column_moved_to_before_lock_is_409(self):
        with self._archived_card_moved_into_limited_column(room=False):
            resp = self._post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.json()["code"], "wip_limit_exceeded")
        self.assertEqual(resp.json()["column_name"], "B")
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNotNone(card.archived_at)
        self.assertEqual(_movements(card, UNARCHIVED), [])
        self.assertEqual(self.hook_events, [])

    def test_restore_into_column_with_room_moved_to_before_lock_succeeds(self):
        with self._archived_card_moved_into_limited_column(room=True):
            resp = self._post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 200, resp.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNone(card.archived_at)
        self.assertEqual(card.column_id, self.col_b.pk)


@skipUnless(
    connection.vendor == "postgresql", "row locks are only observable on PostgreSQL"
)
class CardArchiveConcurrentRaceTests(TransactionTestCase):
    """Real concurrent requests: the first is paused after its read and before
    its lock while the second runs to COMMIT, then the first proceeds."""

    def setUp(self):
        _fixture(self, "racer1605pg")
        patcher = patch("boards.broadcast.broadcast_board_event")
        self.broadcast = patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def _thread(self, name, fn, results):
        def run():
            try:
                results[name] = fn(self._client())
            except Exception as exc:  # surfaced by the assertions below
                results[name] = exc
            finally:
                connections.close_all()

        t = threading.Thread(target=run, name=name)
        t.start()
        return t

    def _response(self, results, name):
        value = results.get(name)
        if isinstance(value, Exception):
            self.fail(f"{name} raised {type(value).__name__}: {value}")
        return value

    def _other_commits_before_lock(self, first, other):
        """``first`` has read the card and is paused just before
        ``lock_card_for_write``; ``other`` runs to COMMIT; then ``first``."""
        paused, release, results = threading.Event(), threading.Event(), {}
        real = card_services.lock_card_for_write

        def wrapper(card_id):
            if threading.current_thread().name == "first" and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)
            return real(card_id)

        with patch(f"{SERVICES}.lock_card_for_write", side_effect=wrapper):
            first_t = self._thread("first", first, results)
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "first never reached its lock")
            other_t = self._thread("other", other, results)
            # Negative check, no lock helper: finishing within HOLD_TIMEOUT proves it never blocked.
            other_t.join(HOLD_TIMEOUT)
            self.assertFalse(other_t.is_alive(), "other blocked on an unlocked request")
            release.set()
            first_t.join(HOLD_TIMEOUT)
            self.assertFalse(first_t.is_alive(), "first never finished")
        return self._response(results, "first"), self._response(results, "other")

    def _archive(self, c):
        return c.post(f"{self.base}/archive/")

    def _unarchive(self, c):
        return c.post(f"{self.base}/unarchive/")

    def _move_to_b(self, c):
        return c.post(
            f"{self.base}/move/",
            {"column_id": self.col_b.pk, "swimlane_id": self.swim.pk, "position": 0},
            format="json",
        )

    def _events(self, event_type):
        return [c for c in self.broadcast.call_args_list if c.args[1] == event_type]

    def test_move_committed_before_archive_lock(self):
        archived, moved = self._other_commits_before_lock(self._archive, self._move_to_b)
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(archived.status_code, 200, archived.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertEqual(card.column_id, self.col_b.pk)
        self.assertIsNotNone(card.archived_at)
        [row] = _movements(card, ARCHIVED)
        # The archive happened in B, not in the A the archive first read.
        self.assertEqual(
            (row.from_column_id, row.to_column_id, row.from_column_name, row.to_column_name),
            (self.col_b.pk, self.col_b.pk, "B", "B"),
        )

    def test_archive_committed_before_archive_lock(self):
        first, other = self._other_commits_before_lock(self._archive, self._archive)
        self.assertEqual(other.status_code, 200, other.data)
        # The loser gets the existing already-archived answer: 200, the card.
        self.assertEqual(first.status_code, 200, first.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNotNone(card.archived_at)
        self.assertEqual(first.data["archived_at"], other.data["archived_at"])
        self.assertEqual(len(_movements(card, ARCHIVED)), 1)
        self.assertEqual(len(self._events(_broadcast.EVT_CARD_ARCHIVED)), 1)
        self.assertEqual(self.hook_events, ["card.archived"])

    def test_unarchive_committed_before_unarchive_lock(self):
        Card.objects.filter(pk=self.card.pk).update(archived_at=timezone.now())
        first, other = self._other_commits_before_lock(self._unarchive, self._unarchive)
        self.assertEqual(other.status_code, 200, other.data)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertIsNone(first.data["archived_at"])
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNone(card.archived_at)
        self.assertEqual(len(_movements(card, UNARCHIVED)), 1)
        self.assertEqual(len(self._events(_broadcast.EVT_CARD_UNARCHIVED)), 1)
        self.assertEqual(self.hook_events, ["card.restored"])
