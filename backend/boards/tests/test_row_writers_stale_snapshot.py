"""Row writers that queue behind a concurrent writer of the same row (#1602).

The first #1602 pass made UPDATE writers lock the board row (and their own
row) so one queued behind a *delete* answers 404. This file covers the
neighbors found by the pre-MR gates, where the earlier request is not a
container delete but another write of the same row:

* ``PATCH`` of a board read the board before taking the row lock and then
  ``save()`` wrote every column back, so a PATCH of ``name`` that queued
  behind a share link change (or a move-group) also wrote the values it had
  read earlier. It now applies only the fields the request sent.
* ``move_group`` read the group the board was leaving before the lock, so two
  queued moves sent ``board.deleted`` to a group the board had already left.
* A custom field definition or label ``PATCH`` queued behind a ``DELETE`` of
  the same row UPDATEd zero rows and ``save()`` fell back to an INSERT under
  the same pk: the deleted row came back.

Each race pauses the first request inside its transaction right after it
took its lock (``_hold_after``), starts the second, and releases. PostgreSQL
only: SQLite has no row locks. ``TestLockBoardAndRowScoping`` runs everywhere.
"""

import json
import threading
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.exceptions import NotFound
from rest_framework.test import APIClient

from accounts.models import User
from boards import broadcast as _broadcast
from boards.models import (
    Board,
    BoardEvent,
    BoardMembership,
    Column,
    CustomFieldDefinition,
    Label,
    Swimlane,
    SwimlaneCustomFieldDefinition,
)
from boards.views._helpers import lock_board_and_row_or_404
from groups.models import Group, GroupMembership
from boards.tests.lock_wait import assert_blocked_on_lock
from boards.tests.test_admin_board_concurrent_edits import _PLAIN_STATIC, page_data

HOLD_TIMEOUT = 10.0


class TestLockBoardAndRowScoping(TestCase):
    """The row is scoped to the board: a pk on another board is a 404."""

    def setUp(self):
        owner = User.objects.create_user(username="o1602s", password="pass")
        self.board = Board.objects.create(name="A", owner=owner)
        self.other = Board.objects.create(name="B", owner=owner)

    def test_row_on_another_board_is_404(self):
        for model, make in (
            (Column, lambda b: Column.objects.create(board=b, name="c", position=0)),
            (Swimlane, lambda b: Swimlane.objects.create(board=b, name="s", position=0)),
            (Label, lambda b: Label.objects.create(board=b, name="l")),
            (
                CustomFieldDefinition,
                lambda b: CustomFieldDefinition.objects.create(
                    board=b, name="f", field_type="text", position=0
                ),
            ),
            (
                SwimlaneCustomFieldDefinition,
                lambda b: SwimlaneCustomFieldDefinition.objects.create(
                    board=b, name="f", field_type="text", position=0
                ),
            ),
        ):
            mine, theirs = make(self.board), make(self.other)
            with transaction.atomic():
                lock_board_and_row_or_404(self.board, model, mine.pk)  # no raise
                with self.assertRaises(NotFound):
                    lock_board_and_row_or_404(self.board, model, theirs.pk)


@skipUnless(
    connection.vendor == "postgresql", "row locks are only observable on PostgreSQL"
)
class _RaceBase(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner1602w", password="pass")
        self.board = Board.objects.create(name="B", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = f"/api/v1/boards/{self.board.pk}"

    def _admin_group(self, name):
        # move-group needs group admin (#1658); a bare owner has no membership.
        group = Group.objects.create(name=name, owner=self.owner)
        GroupMembership.objects.create(
            group=group, user=self.owner, role=GroupMembership.Role.ADMIN
        )
        return group

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        return client

    def _thread(self, name, fn, results):
        def run():
            try:
                results[name] = fn()
            except Exception as exc:  # surfaced by the assertions
                results[name] = exc
            finally:
                connections.close_all()

        t = threading.Thread(target=run, name=name)
        t.start()
        return t

    def _race(self, hold_target, first, second):
        """Run ``first`` (thread "holder") until it holds the lock taken by
        ``hold_target``, start ``second``, then release. Returns the two
        results; the second must have waited."""
        module_path, attr = hold_target.rsplit(".", 1)
        real = getattr(__import__(module_path, fromlist=[attr]), attr)
        paused, release, results = threading.Event(), threading.Event(), {}

        def wrapper(*args, **kwargs):
            out = real(*args, **kwargs)
            if threading.current_thread().name == "holder" and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)
            return out

        with patch(hold_target, side_effect=wrapper):
            holder = self._thread("holder", first, results)
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "first request never held its lock")
            writer = self._thread("writer", second, results)
            assert_blocked_on_lock(self, writer, "second request did not wait")
            release.set()
            holder.join(HOLD_TIMEOUT)
            writer.join(HOLD_TIMEOUT)
        for name in ("holder", "writer"):
            self.assertNotIsInstance(results.get(name), Exception, results.get(name))
        return results["holder"], results["writer"]


class BoardPatchStaleSnapshotTests(_RaceBase):
    """Board PATCH applies only the fields it sent, on the locked row."""

    LOCK = "boards.views.boards.lock_board_row_or_404"

    def test_name_patch_queued_behind_share_change_keeps_share_fields(self):
        client = self._client()
        self.assertEqual(client.post(f"{self.base}/share/", {}, format="json").status_code, 200)
        self.assertIsNotNone(Board.objects.get(pk=self.board.pk).share_token)
        share_change, patch_name = self._race(
            self.LOCK,
            lambda: self._client().delete(f"{self.base}/share/"),
            lambda: self._client().patch(f"{self.base}/", {"name": "Renamed"}, format="json"),
        )
        self.assertEqual(share_change.status_code, 200)
        self.assertEqual(patch_name.status_code, 200, patch_name.content[:300])
        board = Board.objects.get(pk=self.board.pk)
        self.assertEqual(board.name, "Renamed")
        self.assertIsNone(board.share_token, "share fields changed by the other request were overwritten")
        self.assertIsNone(board.share_token_expires_at)

    def test_name_patch_queued_behind_move_group_keeps_the_group(self):
        group = self._admin_group("G")
        move, patch_name = self._race(
            self.LOCK,
            lambda: self._client().post(
                f"{self.base}/move-group/", {"group_id": group.pk}, format="json"
            ),
            lambda: self._client().patch(f"{self.base}/", {"name": "Renamed"}, format="json"),
        )
        self.assertEqual(move.status_code, 200, move.content[:300])
        self.assertEqual(patch_name.status_code, 200, patch_name.content[:300])
        board = Board.objects.get(pk=self.board.pk)
        self.assertEqual(board.name, "Renamed")
        self.assertEqual(board.group_id, group.pk, "move-group was overwritten")


class MoveGroupOldGroupTests(_RaceBase):
    """move_group notifies the group the board actually left."""

    def test_second_move_sends_board_deleted_to_the_group_it_left(self):
        g1, g2, g3 = (self._admin_group(n) for n in ("G1", "G2", "G3"))
        Board.objects.filter(pk=self.board.pk).update(group=g1)
        calls = []
        lock = threading.Lock()

        def record(group_id, event, data):
            with lock:
                calls.append((group_id, event))

        with patch("boards.views.boards._broadcast_group_event", side_effect=record):
            first, second = self._race(
                "boards.views.boards.lock_board_row_or_404",
                lambda: self._client().post(
                    f"{self.base}/move-group/", {"group_id": g2.pk}, format="json"
                ),
                lambda: self._client().post(
                    f"{self.base}/move-group/", {"group_id": g3.pk}, format="json"
                ),
            )
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(Board.objects.get(pk=self.board.pk).group_id, g3.pk)
        deleted = [g for g, e in calls if e == _broadcast.EVT_BOARD_DELETED]
        self.assertEqual(deleted, [g1.pk, g2.pk], calls)
        created = [g for g, e in calls if e == _broadcast.EVT_BOARD_CREATED]
        self.assertEqual(created, [g2.pk, g3.pk], calls)


    def test_patch_behind_a_move_sends_board_deleted_to_the_group_it_left(self):
        """perform_update reads the old group from the locked, refreshed row
        (#1684): a group PATCH queued behind a move-group must tell the group
        the move put the board in, not the one its pre-lock read saw."""
        g1, g2, g3 = (self._admin_group(n) for n in ("P1", "P2", "P3"))
        Board.objects.filter(pk=self.board.pk).update(group=g1)
        calls = []
        lock = threading.Lock()

        def record(group_id, event, data):
            with lock:
                calls.append((group_id, event))

        with patch("boards.views.boards._broadcast_group_event", side_effect=record):
            moved, patched = self._race(
                "boards.views.boards.lock_board_row_or_404",
                lambda: self._client().post(
                    f"{self.base}/move-group/", {"group_id": g2.pk}, format="json"
                ),
                lambda: self._client().patch(
                    f"{self.base}/", {"group": g3.pk}, format="json"
                ),
            )
        self.assertEqual((moved.status_code, patched.status_code), (200, 200), patched.content[:300])
        self.assertEqual(Board.objects.get(pk=self.board.pk).group_id, g3.pk)
        deleted = [g for g, e in calls if e == _broadcast.EVT_BOARD_DELETED]
        self.assertEqual(deleted, [g1.pk, g2.pk], calls)
        created = [g for g, e in calls if e == _broadcast.EVT_BOARD_CREATED]
        self.assertEqual(created, [g2.pk, g3.pk], calls)


@_PLAIN_STATIC
class AdminSaveOldGroupTests(_RaceBase):
    """BoardAdmin.save_model reads the group the board is leaving under the
    board row lock (#1684, #1708), as perform_update does."""

    def test_admin_save_behind_a_move_sends_board_deleted_to_the_group_it_left(self):
        """A form that changes ``group`` with ``row_version`` omitted passes the
        rendered-state check, so only the locked read stands between the save
        and a ``board.deleted`` sent to the group the board left before the
        move. Reading the old group above the lock would tell g1 instead of g2."""
        g1, g2, g3 = (self._admin_group(n) for n in ("A1", "A2", "A3"))
        # The admin form requires a non-empty allowed_priorities.
        Board.objects.filter(pk=self.board.pk).update(group=g1, allowed_priorities=["low"])
        root = User.objects.create_superuser(
            username="root1708", email="root1708@example.com", password="pw123456!x"
        )
        url = reverse("admin:boards_board_change", args=[self.board.pk])
        render_client = Client()
        render_client.force_login(root)
        response = render_client.get(url)
        self.assertEqual(response.status_code, 200)
        data = page_data(response, row_version=False, group=str(g3.pk))
        calls = []
        lock = threading.Lock()

        def record(group_id, event, payload):
            with lock:
                calls.append((group_id, event))

        def admin_save():
            client = Client()
            client.force_login(User.objects.get(pk=root.pk))
            return client.post(url, data)

        with patch("boards.views.boards._broadcast_group_event", side_effect=record):
            moved, saved = self._race(
                "boards.views.boards.lock_board_row_or_404",
                lambda: self._client().post(
                    f"{self.base}/move-group/", {"group_id": g2.pk}, format="json"
                ),
                admin_save,
            )
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(saved.status_code, 302, getattr(saved, "content", b"")[:300])
        self.assertEqual(Board.objects.get(pk=self.board.pk).group_id, g3.pk)
        deleted = [g for g, e in calls if e == _broadcast.EVT_BOARD_DELETED]
        self.assertEqual(deleted, [g1.pk, g2.pk], calls)
        created = [g for g, e in calls if e == _broadcast.EVT_BOARD_CREATED]
        self.assertEqual(created, [g2.pk, g3.pk], calls)


class RestDeleteOldGroupTests(_RaceBase):
    """BoardViewSet.destroy notifies the group the board was in when deleted."""

    def test_rest_delete_behind_a_move_sends_board_deleted_to_the_group_it_was_in(self):
        """BoardViewSet.destroy reads the group from the locked row (#1712):
        get_object() ran before the lock, so a move-group committing meanwhile
        must not leave board.deleted going to the group the board had left."""
        g1, g2 = (self._admin_group(n) for n in ("R1", "R2"))
        Board.objects.filter(pk=self.board.pk).update(group=g1)
        calls = []
        lock = threading.Lock()

        def record(group_id, event, payload):
            with lock:
                calls.append((group_id, event))

        with patch("boards.views.boards._broadcast_group_event", side_effect=record):
            moved, deleted = self._race(
                "boards.views.boards.lock_board_row_or_404",
                lambda: self._client().post(
                    f"{self.base}/move-group/", {"group_id": g2.pk}, format="json"
                ),
                lambda: self._client().delete(f"{self.base}/"),
            )
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", b"")[:300])
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())
        told = [g for g, e in calls if e == _broadcast.EVT_BOARD_DELETED]
        self.assertEqual(told, [g1.pk, g2.pk], calls)


@_PLAIN_STATIC
class AdminDeleteOldGroupTests(_RaceBase):
    """BoardAdmin.delete_model reads the board's group under the board row
    lock (#1712), as save_model does (#1684, #1708)."""

    def test_admin_delete_behind_a_move_sends_board_deleted_to_the_group_it_was_in(self):
        """``delete_view`` reads the board before ``delete_model`` locks it, so
        a move-group that commits in between leaves ``obj.group_id`` stale.
        Reading it above the lock would tell g1 (already told by the move)
        instead of g2, the group the board was in when it was deleted."""
        g1, g2 = (self._admin_group(n) for n in ("D1", "D2"))
        Board.objects.filter(pk=self.board.pk).update(group=g1)
        root = User.objects.create_superuser(
            username="root1712", email="root1712@example.com", password="pw123456!x"
        )
        url = reverse("admin:boards_board_delete", args=[self.board.pk])
        calls = []
        lock = threading.Lock()

        def record(group_id, event, payload):
            with lock:
                calls.append((group_id, event))

        def admin_delete():
            client = Client()
            client.force_login(User.objects.get(pk=root.pk))
            return client.post(url, {"post": "yes"})

        # The move broadcasts through the view's helper, the delete through the admin's.
        with patch("boards.views.boards._broadcast_group_event", side_effect=record), patch(
            "boards.admin._broadcast_group_event", side_effect=record
        ):
            moved, deleted = self._race(
                "boards.views.boards.lock_board_row_or_404",
                lambda: self._client().post(
                    f"{self.base}/move-group/", {"group_id": g2.pk}, format="json"
                ),
                admin_delete,
            )
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(deleted.status_code, 302, getattr(deleted, "content", b"")[:300])
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())
        told = [g for g, e in calls if e == _broadcast.EVT_BOARD_DELETED]
        self.assertEqual(told, [g1.pk, g2.pk], calls)

    def test_bulk_delete_queryset_reads_group_after_the_lock_even_when_cached(self):
        """The admin action evaluates the queryset to build its confirmation
        page, so ``delete_queryset`` receives cached pre-lock rows. It must
        re-read the group from the database under the lock."""
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        g1, g2 = (self._admin_group(n) for n in ("Q1", "Q2"))
        Board.objects.filter(pk=self.board.pk).update(group=g1)
        root = User.objects.create_superuser(
            username="root1712q", email="root1712q@example.com", password="pw123456!x"
        )
        model_admin = django_admin.site._registry[Board]
        queryset = Board.objects.filter(pk=self.board.pk)
        list(queryset)  # cache the pre-move rows, as get_deleted_objects does
        request = RequestFactory().post("/")
        request.user = root
        calls = []

        def record(group_id, event, payload):
            calls.append((group_id, event))

        # The group changes after the queryset was cached, before the delete.
        Board.objects.filter(pk=self.board.pk).update(group=g2)
        # No test transaction wraps this case, so on_commit runs at the outer atomic's exit.
        with patch("boards.admin._broadcast_group_event", side_effect=record):
            model_admin.delete_queryset(request, queryset)
        told = [g for g, e in calls if e == _broadcast.EVT_BOARD_DELETED]
        self.assertEqual(told, [g2.pk], calls)


class DefinitionAndLabelUpdateVsDeleteTests(_RaceBase):
    """A PATCH queued behind a DELETE of the same row answers 404 and does
    not re-insert it (UPDATE matched nothing, ``save()`` fell back to INSERT)."""

    def _check(self, model, make, url_segment, lock_target, payload, update_event):
        obj = make()
        url = f"{self.base}/{url_segment}/{obj.pk}/"
        deleted, patched = self._race(
            lock_target,
            lambda: self._client().delete(url),
            lambda: self._client().patch(url, payload, format="json"),
        )
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(patched.status_code, 404, patched.content[:300])
        self.assertFalse(model.objects.filter(pk=obj.pk).exists(), "deleted row came back")
        events = list(
            BoardEvent.objects.filter(board_id=self.board.pk).values_list("event", flat=True)
        )
        self.assertNotIn(update_event, events, events)

    def test_custom_field_patch_behind_delete(self):
        self._check(
            CustomFieldDefinition,
            lambda: CustomFieldDefinition.objects.create(
                board=self.board, name="Effort", field_type="text", position=0
            ),
            "custom-fields",
            "boards.views.custom_fields.lock_board_and_row_or_404",
            {"name": "Renamed"},
            _broadcast.EVT_CUSTOM_FIELD_UPDATED,
        )

    def test_swimlane_custom_field_patch_behind_delete(self):
        self._check(
            SwimlaneCustomFieldDefinition,
            lambda: SwimlaneCustomFieldDefinition.objects.create(
                board=self.board, name="Tier", field_type="text", position=0
            ),
            "swimlane-custom-fields",
            "boards.views.swimlane_custom_fields.lock_board_and_row_or_404",
            {"name": "Renamed"},
            _broadcast.EVT_SWIMLANE_CUSTOM_FIELD_UPDATED,
        )

    def test_label_patch_behind_delete(self):
        self._check(
            Label,
            lambda: Label.objects.create(board=self.board, name="Bug"),
            "labels",
            "boards.views.labels.lock_board_and_row_or_404",
            {"name": "Renamed"},
            _broadcast.EVT_LABEL_UPDATED,
        )


class DeleteBoardsLockedSetIsFixedTests(TestCase):
    """The boards locked are the boards deleted: the set is resolved once."""

    def test_board_created_after_the_locks_is_not_deleted(self):
        from boards.services import cards as card_services

        owner = User.objects.create_user(username="o1602d", password="pass")
        Board.objects.create(name="wipe-me", owner=owner)
        real = card_services.lock_cards_for_cascade

        def create_late_board(*args, **kwargs):
            out = real(*args, **kwargs)
            Board.objects.create(name="wipe-me", owner=owner)
            return out

        with patch.object(card_services, "lock_cards_for_cascade", side_effect=create_late_board):
            deleted, _ = card_services.delete_boards_locked(
                Board.objects.filter(name="wipe-me")
            )
        self.assertEqual(deleted, 1)
        self.assertEqual(Board.objects.filter(name="wipe-me").count(), 1)


CASCADE = "boards.services.cards.lock_cards_for_cascade"


class CreateWritersVsBoardDeleteTests(_RaceBase):
    """Label create, saved-filter create and star take the board row, so they
    queue with a board delete in both orders instead of committing a row
    between the delete's cascade read and its DELETE (a deferred FK failure at
    COMMIT) or reporting a misleading 400 after it (#1602)."""

    SITES = {
        "label": lambda c, base: c.post(
            f"{base}/labels/", {"name": "L1602", "color": "#112233"}, format="json"
        ),
        "saved_filter": lambda c, base: c.post(
            f"{base}/saved-filters/", {"name": "F1602", "state_json": {}}, format="json"
        ),
        "star": lambda c, base: c.post(f"{base}/star/"),
    }

    def _written(self, site):
        from boards.models import BoardFavorite, SavedFilter

        return {
            "label": lambda: Label.objects.filter(name="L1602").exists(),
            "saved_filter": lambda: SavedFilter.objects.filter(name="F1602").exists(),
            "star": lambda: BoardFavorite.objects.filter(user=self.owner).exists(),
        }[site]()

    def _delete_first(self, site):
        deleted, created = self._race(
            CASCADE,
            lambda: self._client().delete(f"{self.base}/"),
            lambda: self.SITES[site](self._client(), self.base),
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(created.status_code, 404, created.content[:300])
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())
        self.assertFalse(self._written(site))
        events = set(
            BoardEvent.objects.filter(board_id=self.board.pk).values_list("event", flat=True)
        )
        self.assertLessEqual(events, {_broadcast.EVT_BOARD_DELETED}, events)

    def _create_first(self, site):
        created, deleted = self._race(
            "boards.views.%s.lock_board_row_or_404" % ("labels" if site == "label" else "boards"),
            lambda: self.SITES[site](self._client(), self.base),
            lambda: self._client().delete(f"{self.base}/"),
        )
        self.assertIn(created.status_code, (200, 201), created.content[:300])
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())
        self.assertFalse(self._written(site), "row left behind by the board delete")

    def test_label_create_behind_board_delete(self):
        self._delete_first("label")

    def test_saved_filter_create_behind_board_delete(self):
        self._delete_first("saved_filter")

    def test_star_behind_board_delete(self):
        self._delete_first("star")

    def test_board_delete_behind_label_create(self):
        self._create_first("label")

    def test_board_delete_behind_saved_filter_create(self):
        self._create_first("saved_filter")

    def test_board_delete_behind_star(self):
        self._create_first("star")


class DestroyBehindDestroyTests(_RaceBase):
    """A delete that queued behind another delete of the same board, column or
    swimlane answers 404 and stores and broadcasts nothing more (#1602)."""

    def setUp(self):
        super().setUp()
        self.col_a = Column.objects.create(board=self.board, name="A", position=0)
        self.lane = Swimlane.objects.create(board=self.board, name="G", position=0)

    def _events(self, name):
        return BoardEvent.objects.filter(board_id=self.board.pk, event=name).count()

    def _broadcast_count(self, name):
        import boards.broadcast as _b

        return sum(1 for c in _b.broadcast_board_event.call_args_list if c.args[1] == name)

    def test_column_delete_behind_board_delete(self):
        board_del, col_del = self._race(
            CASCADE,
            lambda: self._client().delete(f"{self.base}/"),
            lambda: self._client().delete(f"{self.base}/columns/{self.col_a.pk}/"),
        )
        self.assertEqual(board_del.status_code, 204)
        self.assertEqual(col_del.status_code, 404, col_del.content[:300])
        self.assertEqual(self._events("column.deleted"), 0)
        self.assertEqual(self._broadcast_count("column.deleted"), 0)

    def test_column_delete_behind_column_delete(self):
        first, second = self._race(
            CASCADE,
            lambda: self._client().delete(f"{self.base}/columns/{self.col_a.pk}/"),
            lambda: self._client().delete(f"{self.base}/columns/{self.col_a.pk}/"),
        )
        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 404, second.content[:300])
        self.assertEqual(self._events("column.deleted"), 1)
        self.assertEqual(self._broadcast_count("column.deleted"), 1)

    def test_swimlane_delete_behind_swimlane_delete(self):
        first, second = self._race(
            CASCADE,
            lambda: self._client().delete(f"{self.base}/swimlanes/{self.lane.pk}/"),
            lambda: self._client().delete(f"{self.base}/swimlanes/{self.lane.pk}/"),
        )
        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 404, second.content[:300])
        self.assertEqual(self._events("swimlane.deleted"), 1)
        self.assertEqual(self._broadcast_count("swimlane.deleted"), 1)

    def test_swimlane_delete_behind_board_delete(self):
        board_del, lane_del = self._race(
            CASCADE,
            lambda: self._client().delete(f"{self.base}/"),
            lambda: self._client().delete(f"{self.base}/swimlanes/{self.lane.pk}/"),
        )
        self.assertEqual(board_del.status_code, 204)
        self.assertEqual(lane_del.status_code, 404, lane_del.content[:300])
        self.assertEqual(self._events("swimlane.deleted"), 0)

    def test_second_board_delete_sends_one_board_deleted(self):
        group = Group.objects.create(name="G", owner=self.owner)
        Board.objects.filter(pk=self.board.pk).update(group=group)
        group_calls = []
        with patch(
            "boards.views.boards._broadcast_group_event",
            side_effect=lambda gid, ev, data: group_calls.append((gid, ev)),
        ):
            first, second = self._race(
                CASCADE,
                lambda: self._client().delete(f"{self.base}/"),
                lambda: self._client().delete(f"{self.base}/"),
            )
        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 404, second.content[:300])
        self.assertEqual(self._events("board.deleted"), 1)
        self.assertEqual(self._broadcast_count("board.deleted"), 1)
        self.assertEqual(group_calls, [(group.pk, _broadcast.EVT_BOARD_DELETED)])


class PatchKeepsLockedRowTests(_RaceBase):
    """A PATCH queued behind another write of the same row saves the locked
    row, not the snapshot read before the lock: a rename keeps the position a
    reorder committed (#1602)."""

    def _rename_behind_reorder(self, model, make, segment, lock_target, reorder_url):
        a, b = make("A", 0), make("B", 1)
        order = [b.pk, a.pk]
        reorder, rename = self._race(
            lock_target,
            lambda: self._client().post(
                f"{self.base}/{reorder_url}/", {"order": order}, format="json"
            ),
            lambda: self._client().patch(
                f"{self.base}/{segment}/{a.pk}/", {"name": "Renamed"}, format="json"
            ),
        )
        self.assertEqual(reorder.status_code, 200, reorder.content[:300])
        self.assertEqual(rename.status_code, 200, rename.content[:300])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.name, a.position, b.position), ("Renamed", 1, 0))

    def test_column_rename_behind_reorder(self):
        self._rename_behind_reorder(
            Column,
            lambda n, p: Column.objects.create(board=self.board, name=n, position=p),
            "columns", "boards.views.columns.lock_board_row_or_404", "columns/reorder",
        )

    def test_swimlane_rename_behind_reorder(self):
        self._rename_behind_reorder(
            Swimlane,
            lambda n, p: Swimlane.objects.create(board=self.board, name=n, position=p),
            "swimlanes", "boards.views.swimlanes.lock_board_row_or_404", "swimlanes/reorder",
        )

    def _rename_after_reorder(self, make, segment, lock_target, reorder_url):
        """The custom-field reorders take no board lock, so there is nothing to
        queue behind: hold the PATCH just before its lock instead (after it
        read the row), run the reorder to completion, then release it."""
        a, b = make("A", 0), make("B", 1)
        module_path, attr = lock_target.rsplit(".", 1)
        real = getattr(__import__(module_path, fromlist=[attr]), attr)
        paused, release, results = threading.Event(), threading.Event(), {}

        def wrapper(*args, **kwargs):
            if threading.current_thread().name == "writer":
                paused.set()
                release.wait(HOLD_TIMEOUT)
            return real(*args, **kwargs)

        with patch(lock_target, side_effect=wrapper):
            writer = self._thread(
                "writer",
                lambda: self._client().patch(
                    f"{self.base}/{segment}/{a.pk}/", {"name": "Renamed"}, format="json"
                ),
                results,
            )
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "PATCH never reached its lock")
            reorder = self._client().post(
                f"{self.base}/{reorder_url}/", {"order": [b.pk, a.pk]}, format="json"
            )
            release.set()
            writer.join(HOLD_TIMEOUT)
        self.assertEqual(reorder.status_code, 200, reorder.content[:300])
        rename = results["writer"]
        self.assertNotIsInstance(rename, Exception, rename)
        self.assertEqual(rename.status_code, 200, rename.content[:300])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.name, a.position, b.position), ("Renamed", 1, 0))

    def test_custom_field_rename_after_reorder(self):
        self._rename_after_reorder(
            lambda n, p: CustomFieldDefinition.objects.create(
                board=self.board, name=n, field_type="text", position=p
            ),
            "custom-fields", "boards.views.custom_fields.lock_board_and_row_or_404",
            "custom-fields/reorder",
        )

    def test_swimlane_custom_field_rename_after_reorder(self):
        self._rename_after_reorder(
            lambda n, p: SwimlaneCustomFieldDefinition.objects.create(
                board=self.board, name=n, field_type="text", position=p
            ),
            "swimlane-custom-fields",
            "boards.views.swimlane_custom_fields.lock_board_and_row_or_404",
            "swimlane-custom-fields/reorder",
        )

    def test_label_rename_behind_recolor(self):
        label = Label.objects.create(board=self.board, name="Bug", color="#111111")
        url = f"{self.base}/labels/{label.pk}/"
        recolor, rename = self._race(
            "boards.views.labels.lock_board_and_row_or_404",
            lambda: self._client().patch(url, {"color": "#222222"}, format="json"),
            lambda: self._client().patch(url, {"name": "Renamed"}, format="json"),
        )
        self.assertEqual(recolor.status_code, 200)
        self.assertEqual(rename.status_code, 200, rename.content[:300])
        label.refresh_from_db()
        self.assertEqual((label.name, label.color), ("Renamed", "#222222"))


class BoardAdminSaveTests(TestCase):
    def test_board_admin_save_keeps_non_form_fields_from_locked_row(self):
        import uuid

        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        staff = User.objects.create_superuser(
            username="staff1602a", password="pass", email="s1602a@example.com"
        )
        board = Board.objects.create(
            name="Before", owner=staff, allowed_priorities=["low"]
        )
        model_admin = django_admin.site._registry[Board]
        request = RequestFactory().post("/")
        request.user = staff
        stale = Board.objects.get(pk=board.pk)  # read before the other change
        token = uuid.uuid4()
        Board.objects.filter(pk=board.pk).update(share_token=token)
        form_class = model_admin.get_form(request, stale, change=True)
        # Model fields only: the hidden row_version is not a Board attribute,
        # and a submission without it skips the rendered-state check.
        data = {
            name: getattr(stale, name)
            for name in form_class.base_fields
            if getattr(stale, name, None) is not None
        }
        data["name"] = "After"
        data["allowed_priorities"] = json.dumps(stale.allowed_priorities)
        form = form_class(data=data, instance=stale)
        self.assertTrue(form.is_valid(), form.errors)
        with transaction.atomic():
            model_admin.save_model(request, form.instance, form, True)
        board.refresh_from_db()
        self.assertEqual(board.name, "After")
        self.assertEqual(board.share_token, token)


class SwimlanePatchVsDefinitionDeleteTests(_RaceBase):
    """A swimlane PATCH carrying custom_field_values that queued behind the
    delete of one of those definitions is answered as an unknown definition
    (400), with nothing written, instead of writing a value for a deleted
    definition and reporting a misleading name conflict (#1602)."""

    def test_patch_with_value_for_definition_deleted_while_waiting(self):
        from boards.models import SwimlaneCustomFieldValue

        lane = Swimlane.objects.create(board=self.board, name="G", position=0)
        definition = SwimlaneCustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )
        deleted, patched = self._race(
            "boards.views.swimlane_custom_fields.lock_board_and_row_or_404",
            lambda: self._client().delete(
                f"{self.base}/swimlane-custom-fields/{definition.pk}/"
            ),
            lambda: self._client().patch(
                f"{self.base}/swimlanes/{lane.pk}/",
                {
                    "name": "Renamed",
                    "custom_field_values": [
                        {"field_definition": definition.pk, "value": "gold"}
                    ],
                },
                format="json",
            ),
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(patched.status_code, 400, patched.content[:300])
        self.assertIn("Unknown swimlane custom field", str(patched.json()))
        lane.refresh_from_db()
        self.assertEqual(lane.name, "G")
        self.assertFalse(SwimlaneCustomFieldValue.objects.filter(swimlane=lane).exists())


class AdminDeleteBehindDeleteTests(_RaceBase):
    """An admin single-object delete that queued behind another delete of the
    same object answers the admin's 404 and records no second deletion
    event (#1602)."""

    def setUp(self):
        super().setUp()
        self.objs = {
            "column": Column.objects.create(board=self.board, name="A", position=0),
            "swimlane": Swimlane.objects.create(board=self.board, name="G", position=0),
            "label": Label.objects.create(board=self.board, name="Bug"),
            "board": self.board,
        }
        self.staff = User.objects.create_superuser(
            username="staff1602x", password="pass", email="s1602x@example.com"
        )

    EVENT = {
        "column": "column.deleted", "swimlane": "swimlane.deleted",
        "label": "label.deleted", "board": "board.deleted",
    }
    REST = {"column": "columns", "swimlane": "swimlanes", "label": "labels"}
    LOCK = {
        "column": "boards.admin.lock_cards_for_cascade",
        "swimlane": "boards.admin.lock_cards_for_cascade",
        "board": "boards.admin.lock_cards_for_cascade",
        "label": "boards.admin._lock_board_for_admin_save",
    }
    REST_LOCK = {
        "column": "boards.services.cards.lock_cards_for_cascade",
        "swimlane": "boards.services.cards.lock_cards_for_cascade",
        "board": "boards.services.cards.lock_cards_for_cascade",
        "label": "boards.views.labels.lock_board_and_row_or_404",
    }

    def _admin_delete(self, kind):
        from django.contrib import admin as django_admin
        from django.http import Http404
        from django.test import RequestFactory

        obj = type(self.objs[kind]).objects.get(pk=self.objs[kind].pk)
        model_admin = django_admin.site._registry[type(obj)]

        def run():
            request = RequestFactory().post("/")
            request.user = self.staff
            try:
                with transaction.atomic():
                    model_admin.delete_model(request, obj)
            except Http404:
                return "http404"
            return "deleted"

        return run

    def _rest_delete(self, kind):
        url = (
            f"{self.base}/" if kind == "board"
            else f"{self.base}/{self.REST[kind]}/{self.objs[kind].pk}/"
        )
        return lambda: self._client().delete(url).status_code

    def _check(self, kind, holder_is_admin):
        if holder_is_admin:
            target, first = self.LOCK[kind], self._admin_delete(kind)
        else:
            target, first = self.REST_LOCK[kind], self._rest_delete(kind)
        holder, writer = self._race(target, first, self._admin_delete(kind))
        self.assertIn(holder, ("deleted", 204))
        self.assertEqual(writer, "http404")
        count = BoardEvent.objects.filter(
            board_id=self.board.pk, event=self.EVENT[kind]
        ).count()
        self.assertEqual(count, 1)


def _make_admin_delete_test(kind, holder_is_admin):
    def test(self):
        self._check(kind, holder_is_admin)

    test.__name__ = "test_admin_%s_delete_behind_%s_delete" % (
        kind, "admin" if holder_is_admin else "rest",
    )
    return test


for _kind in ("column", "swimlane", "label", "board"):
    for _holder in (False, True):
        _t = _make_admin_delete_test(_kind, _holder)
        setattr(AdminDeleteBehindDeleteTests, _t.__name__, _t)
