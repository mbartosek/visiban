"""A member add racing the creation of the target's membership (#1706).

``BoardViewSet.members`` reads the target's membership before it takes the
board row lock. A membership created while the request waited for that lock is
then found by ``get_or_create``; the refusal for a site admin's existing
membership must be applied to it there too, not only to the pre-lock read.

PostgreSQL only. To see the bug, remove the ``not created`` re-check in the
view: the request then changes the site admin's role and answers 200.
"""

import threading
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership
from boards.tests.lock_wait import assert_blocked_on_lock

HOLD_TIMEOUT = 10.0


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class MemberAddVsMembershipCreatedTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="race_admin", password="pass")
        self.site_admin = User.objects.create_user(
            username="race_sa", password="pass", is_site_admin=True
        )
        self.board = Board.objects.create(name="R", owner=self.admin)
        BoardMembership.objects.create(board=self.board, user=self.admin, role="admin")

    def test_row_created_while_waiting_for_the_lock_is_still_refused(self):
        holding, release, results = threading.Event(), threading.Event(), {}

        def holder():
            try:
                with transaction.atomic():
                    Board.objects.select_for_update().get(pk=self.board.pk)
                    holding.set()
                    release.wait(HOLD_TIMEOUT)
                    BoardMembership.objects.create(
                        board=self.board, user=self.site_admin, role="admin"
                    )
            finally:
                connections.close_all()

        def requester():
            try:
                client = APIClient()
                client.force_authenticate(self.admin)
                results["resp"] = client.post(
                    f"/api/v1/boards/{self.board.pk}/members/",
                    {"user_id": self.site_admin.pk, "role": "viewer"},
                    format="json",
                )
            except Exception as exc:  # surfaced by the assertions below
                results["resp"] = exc
            finally:
                connections.close_all()

        h = threading.Thread(target=holder)
        h.start()
        self.assertTrue(holding.wait(HOLD_TIMEOUT), "holder never took the board lock")
        r = threading.Thread(target=requester)
        r.start()
        assert_blocked_on_lock(self, r, "the add did not wait for the board lock")
        release.set()
        h.join(HOLD_TIMEOUT)
        r.join(HOLD_TIMEOUT)

        resp = results["resp"]
        self.assertFalse(isinstance(resp, Exception), resp)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            BoardMembership.objects.get(board=self.board, user=self.site_admin).role, "admin"
        )
