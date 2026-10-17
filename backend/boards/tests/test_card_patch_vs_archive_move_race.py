"""A card PATCH racing an archive or a move of the same card (#1598).

``update_card`` locks the card row (#1587), but ``card`` — the instance
``serializer.save()`` writes — was read by ``get_object()`` before the lock,
and ``save()`` writes every column of it. An archive or a move committing in
that window was silently undone: ``PATCH {title}`` answered 200 with
``archived_at`` back to NULL and the card back in its old cell, and no
``CardMovement`` row recorded the reversal.

The fix re-reads the row under the lock (``lock_card_for_write`` returns it,
``_adopt_locked_state`` copies it onto ``card``), then answers as a request
that read the card after the race would: an archived card is a 404, a
title-only PATCH of a moved card keeps the move, and a stale echo of the old
column is ``use_move_endpoint``.

``CardPatchStaleSnapshotTests`` runs on any backend: the archive/move is
applied on the request's own connection just before the lock — the
"committed first" outcome. ``CardPatchVsArchiveMoveRaceTests`` is PostgreSQL
only and uses real concurrent transactions, the PATCH paused after its
``get_object()`` read and before its lock while the other write commits (the
interleaving from the issue), and the reverse order where the PATCH holds the
lock. To see the bug, make ``_adopt_locked_state`` a no-op.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from boards.models import (
    Board,
    BoardMembership,
    Card,
    CardActivity,
    CardMovement,
    Column,
    Swimlane,
)
from boards.services import cards as card_services
from boards.tests.lock_wait import assert_blocked_on_lock
from mcp_server import tools as mcp_tools
from mcp_server.context import reset_current_user, set_current_user

SERVICES = "boards.services.cards"
HOLD_TIMEOUT = 10.0


def _fixture(test, username):
    test.user = User.objects.create_user(username=username, password="pass")
    test.board = Board.objects.create(name="B", owner=test.user)
    BoardMembership.objects.create(
        board=test.board,
        user=test.user,
        role=BoardMembership.Role.ADMIN,
    )
    test.col_a = Column.objects.create(board=test.board, name="A", position=0)
    test.col_b = Column.objects.create(board=test.board, name="B", position=1)
    test.swim = Swimlane.objects.create(board=test.board, name="G", position=0)
    test.card = Card.objects.create(
        board=test.board,
        column=test.col_a,
        swimlane=test.swim,
        title="C",
        created_by=test.user,
        position=0,
    )
    test.base = f"/api/v1/boards/{test.board.pk}/cards/{test.card.pk}"


class CardPatchStaleSnapshotTests(APITestCase):
    """The archive/move lands just before the PATCH's lock, on its connection."""

    def setUp(self):
        _fixture(self, "racer1598")
        self.client.force_authenticate(self.user)

    def _before_lock(self, mutate):
        real = card_services.lock_card_for_write

        def racing(card_id):
            mutate()
            return real(card_id)

        return patch(f"{SERVICES}.lock_card_for_write", side_effect=racing)

    def _archive(self):
        Card.objects.filter(pk=self.card.pk).update(archived_at=timezone.now())

    def _move(self):
        Card.objects.filter(pk=self.card.pk).update(column=self.col_b, position=3)

    def test_patch_of_card_archived_before_lock_returns_404(self):
        with self._before_lock(self._archive):
            resp = self.client.patch(f"{self.base}/", {"title": "again"}, format="json")
        self.assertEqual(resp.status_code, 404, resp.data)
        self.assertFalse(CardActivity.objects.filter(card=self.card).exists())

    def test_patch_of_card_moved_before_lock_keeps_the_move(self):
        with self._before_lock(self._move):
            resp = self.client.patch(f"{self.base}/", {"title": "again"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertEqual(
            (card.column_id, card.position, card.title), (self.col_b.pk, 3, "again")
        )
        self.assertIsNone(card.archived_at)
        self.assertEqual(resp.data["column"], self.col_b.pk)

    def test_stale_echo_of_old_column_is_use_move_endpoint(self):
        """The client round-trips the representation it read before the move:
        under the lock that echo is a change of column, not a no-op."""
        with self._before_lock(self._move):
            resp = self.client.patch(
                f"{self.base}/",
                {"title": "again", "column": self.col_a.pk},
                format="json",
            )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["code"], "use_move_endpoint")

    def test_unraced_echo_of_current_column_still_accepted(self):
        resp = self.client.patch(
            f"{self.base}/",
            {"title": "again", "column": self.col_a.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Card.objects.get(pk=self.card.pk).title, "again")


@skipUnless(
    connection.vendor == "postgresql", "row locks are only observable on PostgreSQL"
)
class CardPatchVsArchiveMoveRaceTests(TransactionTestCase):
    def setUp(self):
        _fixture(self, "racer1598pg")
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
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

    def _pause_in(self, target, thread_name, paused, release):
        """Wrap ``target`` so its first call on ``thread_name`` holds until
        ``release`` is set."""
        module_path, attr = target.rsplit(".", 1)
        real = getattr(__import__(module_path, fromlist=[attr]), attr)

        def wrapper(*args, **kwargs):
            if threading.current_thread().name == thread_name and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)
            return real(*args, **kwargs)

        return patch(target, side_effect=wrapper)

    def _response(self, results, name):
        value = results.get(name)
        if isinstance(value, Exception):
            self.fail(f"{name} raised {type(value).__name__}: {value}")
        return value

    def _archive(self, c):
        return c.post(f"{self.base}/archive/")

    def _move(self, c):
        return c.post(
            f"{self.base}/move/",
            {"column_id": self.col_b.pk, "swimlane_id": self.swim.pk, "position": 0},
            format="json",
        )

    def _patch(self, body):
        return lambda c: c.patch(f"{self.base}/", body, format="json")

    def _other_commits_before_patch_lock(self, other, body):
        """The PATCH has read the card (get_object) and is paused just before
        its lock; ``other`` runs to COMMIT; then the PATCH proceeds."""
        paused, release, results = threading.Event(), threading.Event(), {}
        with self._pause_in(
            f"{SERVICES}.lock_card_for_write", "patcher", paused, release
        ):
            patcher = self._thread("patcher", self._patch(body), results)
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "PATCH never reached its lock")
            other_t = self._thread("other", other, results)
            # Negative check, no lock helper: finishing within HOLD_TIMEOUT proves it never blocked.
            other_t.join(HOLD_TIMEOUT)
            self.assertFalse(
                other_t.is_alive(), "the other write blocked on an unlocked PATCH"
            )
            release.set()
            patcher.join(HOLD_TIMEOUT)
        return self._response(results, "other"), self._response(results, "patcher")

    def _patch_holds_lock_then_other(self, other, body):
        """The PATCH holds the card lock (paused at its render refetch); the
        other write must queue behind it and then apply on top."""
        paused, release, results = threading.Event(), threading.Event(), {}
        with self._pause_in(
            "boards.views._helpers._card_queryset", "patcher", paused, release
        ):
            patcher = self._thread("patcher", self._patch(body), results)
            self.assertTrue(
                paused.wait(HOLD_TIMEOUT), "PATCH never reached its hold point"
            )
            other_t = self._thread("other", other, results)
            assert_blocked_on_lock(self, other_t, "the other write did not wait for the PATCH's lock")
            release.set()
            patcher.join(HOLD_TIMEOUT)
            other_t.join(HOLD_TIMEOUT)
        return self._response(results, "other"), self._response(results, "patcher")

    # -- archive -------------------------------------------------------------

    def test_archive_committed_before_patch_lock_is_not_undone(self):
        archived, patched = self._other_commits_before_patch_lock(
            self._archive, {"title": "again"}
        )
        self.assertEqual(archived.status_code, 200, archived.data)
        # Same answer as a PATCH of an already-archived card: 404.
        self.assertEqual(patched.status_code, 404, patched.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNotNone(card.archived_at)
        self.assertEqual(card.title, "C")
        self.assertFalse(CardActivity.objects.filter(card=card).exists())

    def test_patch_then_archive(self):
        archived, patched = self._patch_holds_lock_then_other(
            self._archive, {"title": "again"}
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(archived.status_code, 200, archived.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNotNone(card.archived_at)
        self.assertEqual(card.title, "again")

    # -- move ----------------------------------------------------------------

    def test_move_committed_before_patch_lock_is_not_undone(self):
        moves_before = CardMovement.objects.filter(card=self.card).count()
        moved, patched = self._other_commits_before_patch_lock(
            self._move, {"title": "again"}
        )
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(patched.status_code, 200, patched.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertEqual((card.column_id, card.title), (self.col_b.pk, "again"))
        self.assertEqual(patched.data["column"], self.col_b.pk)
        # Only the move's own row: the PATCH neither reversed it nor wrote one.
        self.assertEqual(
            CardMovement.objects.filter(card=card).count(), moves_before + 1
        )

    def test_stale_column_echo_after_move_is_use_move_endpoint(self):
        moved, patched = self._other_commits_before_patch_lock(
            self._move,
            {"title": "again", "column": self.col_a.pk},
        )
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(patched.status_code, 400, patched.data)
        self.assertEqual(patched.data["code"], "use_move_endpoint")
        card = Card.objects.get(pk=self.card.pk)
        self.assertEqual((card.column_id, card.title), (self.col_b.pk, "C"))

    def test_patch_then_move(self):
        moved, patched = self._patch_holds_lock_then_other(
            self._move, {"title": "again"}
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(moved.status_code, 200, moved.data)
        card = Card.objects.get(pk=self.card.pk)
        self.assertEqual((card.column_id, card.title), (self.col_b.pk, "again"))


class McpUpdateCardStaleSnapshotTests(APITestCase):
    """The MCP ``update_card`` tool goes through the same service (#1598).

    Called directly rather than over the transport, so it runs where
    ``test_mcp_crud.py`` is skipped. The archive/move is committed right after
    the tool's own read of the card (``_resolve_board_for_card``), outside the
    service's transaction, so it survives a refused update and the test can
    check it was not undone.
    """

    def setUp(self):
        _fixture(self, "racer1598mcp")
        token = set_current_user(self.user)
        self.addCleanup(reset_current_user, token)

    def _after_tool_read(self, mutate):
        real = mcp_tools._resolve_board_for_card

        def racing(user, card_id):
            resolved = real(user, card_id)
            mutate()
            return resolved

        return patch("mcp_server.tools._resolve_board_for_card", side_effect=racing)

    def test_card_archived_after_tool_read_is_not_found(self):
        with self._after_tool_read(
            lambda: Card.objects.filter(pk=self.card.pk).update(
                archived_at=timezone.now()
            )
        ):
            out = mcp_tools.update_card(card_id=self.card.pk, title="again")
        self.assertEqual(out.get("error", {}).get("code"), "card_not_found", out)
        card = Card.objects.get(pk=self.card.pk)
        self.assertIsNotNone(card.archived_at)
        self.assertEqual(card.title, "C")

    def test_card_moved_after_tool_read_keeps_the_move(self):
        with self._after_tool_read(
            lambda: Card.objects.filter(pk=self.card.pk).update(
                column=self.col_b, position=3
            )
        ):
            out = mcp_tools.update_card(card_id=self.card.pk, title="again")
        self.assertNotIn("error", out)
        card = Card.objects.get(pk=self.card.pk)
        self.assertEqual(
            (card.column_id, card.position, card.title), (self.col_b.pk, 3, "again")
        )
