"""#1950 — notification verbs must fit ``Notification.verb`` (max_length=500).

``Card.title`` is also up to 500 characters, so every producer that builds a
verb around the title has to clip it. SQLite does not enforce varchar length,
so these tests only prove the fix on PostgreSQL (CI runs against it); on SQLite
they still assert the clipped length, which is what the column needs.
"""

import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, CardMovement, Column, Notification, Swimlane
from boards.services.notifications import quoted_card_verb
from boards.utils import notify_new_mentions

MAX_LEN = Notification._meta.get_field("verb").max_length
LONG_TITLE = "T" * 500


class QuotedCardVerbSuffixTests(TestCase):
    def test_suffix_is_kept_and_title_clipped(self):
        verb = quoted_card_verb("a moved", LONG_TITLE, " to Done")
        self.assertEqual(len(verb), MAX_LEN)
        self.assertTrue(verb.startswith('a moved "T'))
        self.assertTrue(verb.endswith('…" to Done'))

    def test_empty_lead_has_no_leading_space(self):
        self.assertEqual(quoted_card_verb("", "Fix", " is due soon"), '"Fix" is due soon')

    def test_short_inputs_unchanged(self):
        self.assertEqual(quoted_card_verb("You were assigned to", "Fix"), 'You were assigned to "Fix"')

    def test_oversized_suffix_is_clipped_as_last_resort(self):
        verb = quoted_card_verb("a moved", LONG_TITLE, " to " + "C" * 600)
        self.assertLessEqual(len(verb), MAX_LEN)
        self.assertIn('a moved "', verb)


class NotificationVerbOverflowTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner1950", password="pass")
        self.assignee = User.objects.create_user(
            username="assignee1950",
            password="pass",
            notif_stale=True,
            notif_due_soon=True,
            notif_card_moved=True,
            notif_card_assigned=True,
        )
        self.board = Board.objects.create(name="B" * 255, owner=self.owner)
        for user, role in ((self.owner, BoardMembership.Role.ADMIN), (self.assignee, BoardMembership.Role.MEMBER)):
            BoardMembership.objects.create(board=self.board, user=user, role=role)
        self.col_a = Column.objects.create(board=self.board, name="Backlog", position=0)
        self.col_b = Column.objects.create(board=self.board, name="D" * 100, position=1)
        self.swim = Swimlane.objects.create(board=self.board, name="General", position=0)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _card(self, **extra):
        return Card.objects.create(
            board=self.board,
            column=self.col_a,
            swimlane=self.swim,
            title=LONG_TITLE,
            created_by=self.owner,
            position=0,
            **extra,
        )

    def _only_verb(self, card, action_type):
        # Reading the row back proves the INSERT succeeded (PostgreSQL raises
        # DataError on an over-long varchar; SQLite would not).
        verb = Notification.objects.get(card=card, action_type=action_type).verb
        self.assertLessEqual(len(verb), MAX_LEN)
        self.assertIn("T" * 50, verb)  # still names the card
        return verb

    def test_stale_notification_with_long_title_and_board_name(self):
        card = self._card()
        mv = CardMovement.objects.create(
            card=card, to_column=self.col_a, to_swimlane=self.swim, moved_by=self.owner
        )
        stale = timezone.now() - datetime.timedelta(days=self.board.staleness_threshold_days + 1)
        CardMovement.objects.filter(pk=mv.pk).update(moved_at=stale)
        self.owner.notif_stale = True
        self.owner.save()

        call_command("notify_stale_cards", stdout=StringIO(), verbosity=0)

        verb = self._only_verb(card, Notification.ActionType.STALE)
        self.assertIn("hasn't moved in", verb)
        self.assertIn("(board: ", verb)

    def test_one_long_title_does_not_abort_stale_scan_for_other_cards(self):
        self._card()
        short = Card.objects.create(
            board=self.board, column=self.col_a, swimlane=self.swim, title="Short", created_by=self.owner, position=1
        )
        old = timezone.now() - datetime.timedelta(days=self.board.staleness_threshold_days + 5)
        Card.objects.filter(pk__in=[c.pk for c in Card.objects.all()]).update(created_at=old)
        self.owner.notif_stale = True
        self.owner.save()

        call_command("notify_stale_cards", stdout=StringIO(), verbosity=0)

        self.assertTrue(Notification.objects.filter(card=short).exists())

    def test_due_soon_with_long_title(self):
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        card = self._card(assignee=self.assignee, due_date=tomorrow)

        with self.captureOnCommitCallbacks(execute=True):
            call_command("notify_due_soon", stdout=StringIO())

        verb = self._only_verb(card, Notification.ActionType.DUE_SOON)
        self.assertTrue(verb.endswith('" is due soon'))

    @patch("boards.broadcast.broadcast_board_event")
    def test_card_move_with_long_title(self, _broadcast):
        card = self._card(assignee=self.assignee)

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                f"/api/v1/boards/{self.board.pk}/cards/{card.pk}/move/",
                {"column_id": self.col_b.pk, "swimlane_id": self.swim.pk, "position": 0},
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        verb = self._only_verb(card, Notification.ActionType.CARD_MOVED)
        self.assertTrue(verb.endswith(f'" to {self.col_b.name}'))

    @patch("boards.broadcast.broadcast_board_event")
    def test_assignment_with_long_title(self, _broadcast):
        card = self._card()

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.patch(
                f"/api/v1/boards/{self.board.pk}/cards/{card.pk}/", {"assignee_id": self.assignee.pk}
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        verb = self._only_verb(card, Notification.ActionType.ASSIGNED)
        self.assertTrue(verb.startswith("You were assigned to "))

    def test_description_mention_with_long_title(self):
        card = self._card()

        notify_new_mentions(card, self.owner, "", f"@{self.assignee.username} look")

        verb = self._only_verb(card, Notification.ActionType.MENTIONED)
        self.assertTrue(verb.startswith("owner1950 mentioned you in "))
