"""JSON import restores a card's exported ``created_at`` (#1906).

An imported card used to be dated to the import, so its restored history
predated the card itself. The importer now honors the file's ``created_at``
when it is one the card could have had, and falls back to the import time
otherwise (absent, unparseable, in the future, or after the card's own first
history entry).
"""

import datetime
import io
import json

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, Card
from boards.tests.history_reconciliation import board_history_problems

URL = "/api/v1/boards/import/"
MOVED = "2024-03-04T05:06:07+00:00"
ACTED = "2024-03-04T06:00:00+00:00"
COMMENTED = "2024-03-04T07:00:00+00:00"


def _payload(**card):
    return {
        "name": "Dated",
        "columns": [{"name": "To Do", "position": 0}],
        "swimlanes": [{"name": "General", "position": 0}],
        "labels": [],
        "cards": [
            {
                "title": "Card",
                "column": "To Do",
                "swimlane": "General",
                "position": 0,
                "movements": [{"to_column": "To Do", "moved_by": "alice", "moved_at": MOVED, "notes": "Card created"}],
                "activities": [
                    {
                        "event_type": "priority_change", "from_value": "medium", "to_value": "high",
                        "actor": "alice", "created_at": ACTED,
                    }
                ],
                "comments": [{"author": "alice", "body": "hi", "created_at": COMMENTED}],
                **card,
            }
        ],
    }


class ImportCardCreatedAtTests(TestCase):
    def setUp(self):
        cache.clear()
        self.importer = User.objects.create_user(username="importer", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.importer)

    def _import(self, data, **extra):
        cache.clear()
        f = io.BytesIO(json.dumps(data).encode("utf-8"))
        f.name = "board.json"
        resp = self.client.post(URL, {"file": f, **extra}, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Card.objects.get(board_id=resp.data["id"])

    def _assert_fell_back_to_now(self, card, before):
        self.assertGreaterEqual(card.created_at, before)
        self.assertLessEqual(card.created_at, timezone.now())

    def test_valid_created_at_is_restored(self):
        card = self._import(_payload(created_at="2024-03-04T05:06:00+00:00"))
        self.assertEqual(card.created_at, datetime.datetime(2024, 3, 4, 5, 6, tzinfo=datetime.timezone.utc))

    def test_created_at_equal_to_first_history_entry_is_restored(self):
        card = self._import(_payload(created_at=MOVED))
        self.assertEqual(card.created_at.isoformat(), MOVED)

    def test_naive_created_at_is_read_as_server_time_zone(self):
        card = self._import(_payload(created_at="2024-03-04T05:00:00"))
        self.assertEqual(card.created_at.year, 2024)
        self.assertLess(card.created_at, datetime.datetime(2024, 3, 4, 6, tzinfo=datetime.timezone.utc))

    def test_missing_created_at_imports_at_now(self):
        before = timezone.now()
        card = self._import(_payload())
        self._assert_fell_back_to_now(card, before)

    def test_future_created_at_falls_back_to_now(self):
        before = timezone.now()
        future = (before + datetime.timedelta(days=2)).isoformat()
        card = self._import(_payload(created_at=future, movements=[], activities=[], comments=[]))
        self._assert_fell_back_to_now(card, before)

    def test_created_at_after_first_movement_falls_back_to_now(self):
        before = timezone.now()
        card = self._import(_payload(created_at="2024-03-04T05:06:08+00:00"))
        self._assert_fell_back_to_now(card, before)

    def test_created_at_after_first_activity_or_comment_falls_back_to_now(self):
        for key, entries in (
            ("movements", []),  # only the activity (06:00) and comment (07:00) remain
        ):
            with self.subTest(key=key):
                before = timezone.now()
                card = self._import(_payload(created_at="2024-03-04T06:30:00+00:00", **{key: entries}))
                self._assert_fell_back_to_now(card, before)
        before = timezone.now()
        card = self._import(
            _payload(created_at="2024-03-04T07:30:00+00:00", movements=[], activities=[])
        )
        self._assert_fell_back_to_now(card, before)

    def test_history_off_ignores_history_for_the_ordering_check(self):
        # Nothing of the file's history is kept, so nothing can contradict it.
        card = self._import(
            _payload(created_at="2024-03-04T08:00:00+00:00"),
            options=json.dumps({"history": False}),
        )
        self.assertEqual(card.created_at.isoformat(), "2024-03-04T08:00:00+00:00")

    def test_unusable_created_at_falls_back_to_now_without_rejecting_the_file(self):
        # Files that carried a bad created_at always imported (the field was
        # ignored); rejecting them now would break them, so they keep importing.
        for bad in ("not-a-date", "2024-02-30T00:00:00Z", 20240304, ["x"], {"a": 1}):
            with self.subTest(bad=bad):
                before = timezone.now()
                card = self._import(_payload(created_at=bad))
                self._assert_fell_back_to_now(card, before)

    def test_shifted_dates_move_created_at_with_the_history(self):
        anchor = (timezone.localdate() - datetime.timedelta(days=10)).isoformat()
        stamp = (timezone.now() - datetime.timedelta(days=10, hours=3)).isoformat()
        data = _payload(created_at=stamp)
        data["cards"][0]["movements"][0]["moved_at"] = (
            timezone.now() - datetime.timedelta(days=10, hours=2)
        ).isoformat()
        data["cards"][0]["activities"] = []
        data["cards"][0]["comments"] = []
        card = self._import(data, options=json.dumps({"shift_dates_from": anchor}))
        self.assertLess(timezone.now() - card.created_at, datetime.timedelta(hours=4))
        self.assertLess(card.created_at, card.movements.get().moved_at)

    def test_export_then_import_preserves_created_at_and_reconciles(self):
        source = self._import_via_api_board()
        exported = self.client.get(f"/api/v1/boards/{source.pk}/export/?format=json")
        self.assertEqual(exported.status_code, 200)
        original = {c["title"]: c["created_at"] for c in json.loads(exported.content)["cards"]}
        self.assertTrue(all(original.values()))

        cache.clear()
        f = io.BytesIO(exported.content)
        f.name = "board.json"
        resp = self.client.post(URL, {"file": f}, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        copy = Board.objects.get(pk=resp.data["id"])
        restored = {c.title: c.created_at for c in copy.cards.all()}
        for title, iso in original.items():
            self.assertEqual(restored[title], datetime.datetime.fromisoformat(iso), title)
        self.assertEqual(board_history_problems(copy, imported=True), {})

    def _import_via_api_board(self):
        """A board whose cards were made through the API, so each has a real creation movement."""
        board = Board.objects.create(name="Source", owner=self.importer)
        from boards.models import BoardMembership, Column, Swimlane

        BoardMembership.objects.create(board=board, user=self.importer, role=BoardMembership.Role.ADMIN)
        column = Column.objects.create(board=board, name="To Do", position=0, allow_card_creation=True)
        swimlane = Swimlane.objects.create(board=board, name="General", position=0)
        for title in ("One", "Two"):
            resp = self.client.post(
                f"/api/v1/boards/{board.pk}/cards/",
                {"title": title, "column": column.pk, "swimlane": swimlane.pk},
                format="json",
            )
            self.assertEqual(resp.status_code, 201, resp.content)
        # Make the originals older than "now" by a margin that a fallback could not fake.
        old = timezone.now() - datetime.timedelta(days=30)
        for card in board.cards.all():
            Card.objects.filter(pk=card.pk).update(created_at=old)
            card.movements.update(moved_at=old)
            card.activities.update(created_at=old)
        return board
