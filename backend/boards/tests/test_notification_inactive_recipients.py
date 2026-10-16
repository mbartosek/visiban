"""Inactive users are never notification recipients (#1686).

``create_notifications`` is the single funnel every producer goes through, so
the active-user filter lives there. These tests drive each producer end to end
and assert the same thing for each: an inactive member gets no row and no
``post_notification_created`` signal, while an active member in the identical
situation still does (positive control, so a broken fixture cannot pass the
negative assertion vacuously).
"""
import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, Column, Notification, Swimlane
from boards.services.notifications import create_notifications
from boards.signals import post_notification_created
from boards.utils import notify_new_mentions


class InactiveRecipientTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own1686", password="pass")
        self.board = Board.objects.create(name="B1686", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        self.col = Column.objects.create(
            board=self.board, name="Backlog", position=0, allow_card_creation=True
        )
        self.swim = Swimlane.objects.create(board=self.board, name="General", position=0)
        self.active = self._member("act1686", notif_comment_added=True)
        self.inactive = self._member("ina1686", is_active=False, notif_comment_added=True)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

        self.received = []

        def _receiver(sender, notification=None, **kwargs):
            self.received.append(notification)

        post_notification_created.connect(_receiver, dispatch_uid="test_inactive_1686", weak=False)
        self.addCleanup(post_notification_created.disconnect, dispatch_uid="test_inactive_1686")

    def _member(self, username, **extra):
        user = User.objects.create_user(username=username, password="pass", **extra)
        BoardMembership.objects.create(
            board=self.board, user=user, role=BoardMembership.Role.MEMBER
        )
        return user

    def _card(self, **extra):
        return Card.objects.create(
            board=self.board, column=self.col, swimlane=self.swim,
            title="Card 1686", created_by=self.owner, position=0, **extra,
        )

    def _assert_recipient(self, user, expected):
        rows = Notification.objects.filter(recipient=user).exists()
        signalled = any(n.recipient_id == user.pk for n in self.received)
        self.assertEqual(rows, expected)
        self.assertEqual(signalled, expected)

    # -- funnel ------------------------------------------------------------

    def test_funnel_drops_inactive_and_keeps_active(self):
        card = self._card()
        batch = [
            Notification(
                recipient=u, actor=self.owner, card=card, board=self.board,
                action_type=Notification.ActionType.ASSIGNED, verb="x",
            )
            for u in (self.active, self.inactive)
        ]
        with self.captureOnCommitCallbacks(execute=True):
            created = create_notifications(batch)
        self.assertEqual([n.recipient_id for n in created], [self.active.pk])
        self._assert_recipient(self.active, True)
        self._assert_recipient(self.inactive, False)

    def test_funnel_filter_is_one_bulk_query(self):
        card = self._card()
        users = [self._member(f"bulk1686_{i}") for i in range(5)]
        batch = [
            Notification(
                recipient_id=u.pk, card=card, board=self.board,
                action_type=Notification.ActionType.ASSIGNED, verb="x",
            )
            for u in users
        ]
        with self.assertNumQueries(2):  # one active-user lookup + one bulk insert
            create_notifications(batch)

    # -- producers ---------------------------------------------------------

    def test_due_soon(self):
        for user in (self.active, self.inactive):
            user.notif_due_soon = True
            user.save()
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        a = self._card(assignee=self.active, due_date=tomorrow)
        b = self._card(assignee=self.inactive, due_date=tomorrow)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("notify_due_soon", stdout=StringIO())
        self.assertTrue(Notification.objects.filter(recipient=self.active, card=a).exists())
        self.assertFalse(Notification.objects.filter(card=b).exists())
        self._assert_recipient(self.inactive, False)

    def _comment(self, card, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/v1/boards/{self.board.pk}/cards/{card.pk}/comments/", {"body": body}
            )

    @patch("boards.broadcast.broadcast_board_event")
    def test_comment_mention(self, _bc):
        card = self._card()
        resp = self._comment(card, "hi @act1686 and @ina1686")
        self.assertEqual(resp.status_code, 201)
        self._assert_recipient(self.active, True)
        self._assert_recipient(self.inactive, False)

    @patch("boards.broadcast.broadcast_board_event")
    def test_comment_watcher(self, _bc):
        """Regression guard: the watcher query already filtered inactive users."""
        # The assignee is an implicit watcher of the card.
        card_active = self._card(assignee=self.active)
        self._comment(card_active, "plain comment")
        self._assert_recipient(self.active, True)

        card_inactive = self._card(assignee=self.inactive)
        self._comment(card_inactive, "plain comment")
        self._assert_recipient(self.inactive, False)

    def test_description_mention(self):
        card = self._card()
        with self.captureOnCommitCallbacks(execute=True):
            notify_new_mentions(card, self.owner, "", "cc @act1686 @ina1686")
        self._assert_recipient(self.active, True)
        self._assert_recipient(self.inactive, False)

    @patch("boards.broadcast.broadcast_board_event")
    def test_assignment(self, _bc):
        card = self._card()
        for user in (self.active, self.inactive):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.patch(
                    f"/api/v1/boards/{self.board.pk}/cards/{card.pk}/",
                    {"assignee_id": user.pk},
                )
            self.assertEqual(resp.status_code, 200)
        self._assert_recipient(self.active, True)
        self._assert_recipient(self.inactive, False)

    @patch("boards.broadcast.broadcast_board_event")
    def test_board_invite(self, _bc):
        outsiders = {
            True: User.objects.create_user(username="inv_act1686", password="pass"),
            False: User.objects.create_user(
                username="inv_ina1686", password="pass", is_active=False
            ),
        }
        for user in outsiders.values():
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    f"/api/v1/boards/{self.board.pk}/members/",
                    {"user_id": user.pk, "role": "member"},
                )
        self._assert_recipient(outsiders[True], True)
        self._assert_recipient(outsiders[False], False)
