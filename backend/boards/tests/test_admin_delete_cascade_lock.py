"""Django admin deletes lock the cascaded cards first (#1603).

The admin-site twin of ``test_container_delete_vs_card_write_lock`` (#1599).
Deleting a card, column, swimlane or board, or the user who owns a board,
cascades to cards and their child rows. Without the card-row lock a child write
holding ``lock_card_for_write`` could insert a row the collector had not seen
and commit before the container's card ``DELETE``; the card foreign keys are
``DEFERRABLE INITIALLY DEFERRED`` on PostgreSQL, so the delete then failed its
FK check at COMMIT. Each admin's ``delete_model`` and ``delete_queryset`` now
call ``lock_cards_for_cascade`` inside one ``transaction.atomic()`` (a bulk
admin action is not otherwise wrapped, so a per-object lock would not outlive
the delete).

Tests are generated per (admin class x single/bulk x either order):
``write_then_delete`` (the delete queues, the write's row is cascaded away)
and ``delete_then_write`` (the write queues, finds the card gone, 404).
PostgreSQL only. To see the bug, make ``lock_cards_for_cascade`` a no-op in
``boards.admin`` / ``accounts.admin``: every ``write_then_delete`` test fails.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.db import connection, connections
from django.test import RequestFactory, TransactionTestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import (
    Board,
    BoardMembership,
    Card,
    CardActivity,
    CardChecklist,
    CardComment,
    Column,
    Swimlane,
)

HOLD_TIMEOUT = 10.0
BLOCK_PROBE = 0.5

KINDS = ("card", "column", "swimlane", "board", "user")
MODES = ("model", "queryset")


@skipUnless(
    connection.vendor == "postgresql", "row locks are only observable on PostgreSQL"
)
class AdminDeleteCascadeLockTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="staff1603", password="pass", email="s1603@example.com"
        )
        self.owner = User.objects.create_user(username="owner1603", password="pass")
        self.user = User.objects.create_user(username="racer1603", password="pass")
        self.board = Board.objects.create(name="B", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.user, role=BoardMembership.Role.ADMIN
        )
        self.column = Column.objects.create(board=self.board, name="A", position=0)
        self.swimlane = Swimlane.objects.create(board=self.board, name="G", position=0)
        self.first = Card.objects.create(
            board=self.board, column=self.column, swimlane=self.swimlane,
            title="first", created_by=self.user, position=0,
        )
        self.card = Card.objects.create(
            board=self.board, column=self.column, swimlane=self.swimlane,
            title="raced", created_by=self.user, position=1,
        )
        self.item = CardChecklist.objects.create(
            card=self.card, text="todo", created_by=self.user, position=0
        )
        self.card_base = f"/api/v1/boards/{self.board.pk}/cards/{self.card.pk}"
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers --------------------------------------------------------------

    def _target(self, kind):
        """(model admin, model class, pk) of the object the admin deletes."""
        obj = {
            "card": self.card, "column": self.column, "swimlane": self.swimlane,
            "board": self.board, "user": self.owner,
        }[kind]
        return django_admin.site._registry[type(obj)], type(obj), obj.pk

    def _admin_delete(self, kind, mode):
        model_admin, model, pk = self._target(kind)
        request = RequestFactory().post("/")
        request.user = self.staff
        # A board owner's delete is refused by the user-delete guard (#1616,
        # tested in accounts); this test is about the card lock the cascade
        # takes, so the guard is stubbed out for the user kind.
        with patch("accounts.admin.user_deletion_refusals", return_value=[]):
            if mode == "model":
                model_admin.delete_model(request, model.objects.get(pk=pk))
            else:
                model_admin.delete_queryset(request, model.objects.filter(pk=pk))
        return None

    def _write(self, client):
        return client.post(f"{self.card_base}/comments/", {"body": "hi"}, format="json")

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def _thread(self, name, fn, results):
        def run():
            try:
                results[name] = fn()
            except Exception as exc:  # surfaced by _assert_ok / _status
                results[name] = exc
            finally:
                connections.close_all()

        t = threading.Thread(target=run, name=name)
        t.start()
        return t

    def _pause_in(self, target, thread_name, paused, release, *, after=False):
        module_path, attr = target.rsplit(".", 1)
        real = getattr(__import__(module_path, fromlist=[attr]), attr)

        def hold():
            if threading.current_thread().name == thread_name and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)

        def wrapper(*args, **kwargs):
            if not after:
                hold()
            result = real(*args, **kwargs)
            if after:
                hold()
            return result

        return patch(target, side_effect=wrapper)

    def _assert_deleter_ok(self, results):
        value = results.get("deleter", "missing")
        if isinstance(value, Exception):
            self.fail(f"admin delete raised {type(value).__name__}: {value}")
        self.assertNotEqual(value, "missing")

    def _assert_writer_status(self, results, expected):
        value = results.get("writer")
        if isinstance(value, Exception):
            self.fail(f"writer raised {type(value).__name__}: {value}")
        self.assertEqual(value.status_code, expected)

    def _assert_gone(self, kind):
        pks = [self.card.pk] if kind == "card" else [self.first.pk, self.card.pk]
        self.assertFalse(Card.objects.filter(pk__in=pks).exists())
        self.assertFalse(CardComment.objects.filter(card_id__in=pks).exists())
        self.assertFalse(CardChecklist.objects.filter(card_id__in=pks).exists())
        self.assertFalse(CardActivity.objects.filter(card_id__in=pks).exists())
        _, model, pk = self._target(kind)
        self.assertFalse(model.objects.filter(pk=pk).exists())

    # -- the two orders -------------------------------------------------------

    def _write_then_delete(self, kind, mode):
        paused, release, results = threading.Event(), threading.Event(), {}
        # The refetch runs after the write's insert and before its COMMIT.
        with self._pause_in(
            "boards.views._helpers._card_queryset", "writer", paused, release
        ):
            writer = self._thread("writer", lambda: self._write(self._client()), results)
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "writer never reached its hold")
            deleter = self._thread(
                "deleter", lambda: self._admin_delete(kind, mode) or True, results
            )
            deleter.join(BLOCK_PROBE)
            self.assertTrue(
                deleter.is_alive(), "admin delete did not wait for the card lock"
            )
            release.set()
            writer.join(HOLD_TIMEOUT)
            deleter.join(HOLD_TIMEOUT)
        self._assert_writer_status(results, 201)
        self._assert_deleter_ok(results)
        self._assert_gone(kind)

    def _delete_then_write(self, kind, mode):
        # Patch both admin modules' imported name; only the one in use is hit.
        target = "accounts.admin.lock_cards_for_cascade" if kind == "user" else (
            "boards.admin.lock_cards_for_cascade"
        )
        paused, release, results = threading.Event(), threading.Event(), {}
        with self._pause_in(target, "deleter", paused, release, after=True):
            deleter = self._thread(
                "deleter", lambda: self._admin_delete(kind, mode) or True, results
            )
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "delete never reached its hold")
            writer = self._thread("writer", lambda: self._write(self._client()), results)
            writer.join(BLOCK_PROBE)
            self.assertTrue(writer.is_alive(), "write did not wait for the card lock")
            release.set()
            deleter.join(HOLD_TIMEOUT)
            writer.join(HOLD_TIMEOUT)
        self._assert_deleter_ok(results)
        self._assert_writer_status(results, 404)
        self._assert_gone(kind)


def _make(order, kind, mode):
    def test(self):
        getattr(self, f"_{order}")(kind, mode)

    test.__name__ = f"test_{kind}_admin_{mode}_{order}"
    return test


for _order in ("write_then_delete", "delete_then_write"):
    for _kind in KINDS:
        for _mode in MODES:
            _t = _make(_order, _kind, _mode)
            setattr(AdminDeleteCascadeLockTests, _t.__name__, _t)
