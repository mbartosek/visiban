"""Admin comment/checklist saves hold the parent card's row lock (#1585).

``_CardChildBroadcastAdminMixin.save_model`` inserts a child row whose card
foreign key is ``DEFERRABLE INITIALLY DEFERRED`` on PostgreSQL. Without a card
lock, a card delete committing before the admin transaction's COMMIT fails the
FK check there (a 500). With ``lock_card_for_write`` the delete queues behind the
save (and cascades the new row away), or, if it commits first, the save answers
404. PostgreSQL only. To see the bug, remove the lock call in
``boards.admin._CardChildBroadcastAdminMixin.save_model``:
``test_delete_queues_behind_save`` fails.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.db import connection, connections, transaction
from django.http import Http404
from django.test import RequestFactory, TransactionTestCase

from accounts.models import User
from boards.admin import CardAdmin, CardChecklistAdmin, CardCommentAdmin
from boards.models import (
    Board, BoardMembership, Card, CardChecklist, CardComment, Column, Swimlane,
)

HOLD_TIMEOUT = 10.0
BLOCK_PROBE = 0.5


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class AdminChildSaveCardLockTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="staff1585", password="pass", email="s1585@example.com"
        )
        self.board = Board.objects.create(name="B", owner=self.staff)
        BoardMembership.objects.create(
            board=self.board, user=self.staff, role=BoardMembership.Role.ADMIN
        )
        col = Column.objects.create(board=self.board, name="A", position=0)
        swim = Swimlane.objects.create(board=self.board, name="G", position=0)
        self.card = Card.objects.create(
            board=self.board, column=col, swimlane=swim, title="raced",
            created_by=self.staff, position=0,
        )
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _request(self):
        request = RequestFactory().post("/")
        request.user = self.staff
        return request

    def _new_child(self, kind):
        if kind == "comment":
            return CardCommentAdmin(CardComment, AdminSite()), CardComment(
                card=self.card, author=self.staff, body="hi")
        return CardChecklistAdmin(CardChecklist, AdminSite()), CardChecklist(
            card=self.card, text="t", created_by=self.staff, position=0)

    def _admin_save(self, kind):
        adm, obj = self._new_child(kind)
        # The real changeform view wraps save_model in one transaction; the
        # lock must outlive save_model until that transaction commits.
        with transaction.atomic():
            adm.save_model(self._request(), obj, None, change=False)
            self._after_save()

    def _after_save(self):
        """Hold the save open (row inserted, not committed) until released."""
        hold = getattr(self, "_hold", None)
        if hold and threading.current_thread().name == "writer":
            hold[0].set()
            hold[1].wait(HOLD_TIMEOUT)

    def _admin_delete_card(self):
        CardAdmin(Card, AdminSite()).delete_model(self._request(), Card.objects.get(pk=self.card.pk))
        return True

    def _thread(self, name, fn, results):
        def run():
            try:
                results[name] = fn()
            except Exception as exc:
                results[name] = exc
            finally:
                connections.close_all()

        t = threading.Thread(target=run, name=name)
        t.start()
        return t

    def test_delete_queues_behind_save(self):
        for kind in ("comment", "checklist"):
            with self.subTest(kind=kind):
                self.card = Card.objects.get(pk=self.card.pk)
                paused, release, results = threading.Event(), threading.Event(), {}
                self._hold = (paused, release)
                writer = self._thread("writer", lambda: self._admin_save(kind) or True, results)
                self.assertTrue(paused.wait(HOLD_TIMEOUT), "writer never reached its hold")
                deleter = self._thread("deleter", self._admin_delete_card, results)
                deleter.join(BLOCK_PROBE)
                self.assertTrue(deleter.is_alive(), "card delete did not wait for the save's lock")
                release.set()
                writer.join(HOLD_TIMEOUT)
                deleter.join(HOLD_TIMEOUT)
                self.assertIs(results.get("writer"), True, results)
                self.assertIs(results.get("deleter"), True, results)
                self.assertFalse(Card.objects.filter(pk=self.card.pk).exists())
                self.assertFalse(CardComment.objects.filter(card_id=self.card.pk).exists())
                self.assertFalse(CardChecklist.objects.filter(card_id=self.card.pk).exists())
                # Recreate the card for the next kind.
                self.card = Card.objects.create(
                    board=self.board, column=Column.objects.first(),
                    swimlane=Swimlane.objects.first(), title="raced",
                    created_by=self.staff, position=0,
                )

    def test_save_after_committed_delete_is_404(self):
        Card.objects.filter(pk=self.card.pk).delete()
        for kind in ("comment", "checklist"):
            adm, obj = self._new_child(kind)
            with self.subTest(kind=kind):
                with self.assertRaises(Http404):
                    with transaction.atomic():
                        adm.save_model(self._request(), obj, None, change=False)
        self.assertFalse(CardComment.objects.exists())
        self.assertFalse(CardChecklist.objects.exists())
