"""Out-of-range ``offset`` is a 400 (or clamped), never a 500 (#1696).

Same class as #1669 but for pagination offsets: a value past the 64-bit range
overflowed in the DB driver on archived cards, card timeline and board
movements. Negative offsets clamp to 0; non-numeric stay 0 (long-standing).
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, Column, Swimlane

HUGE = 10**30


class OffsetRangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="adm", password="pass")
        self.outsider = User.objects.create_user(username="out", password="pass")
        self.board = Board.objects.create(name="B", owner=self.user)
        BoardMembership.objects.create(board=self.board, user=self.user, role=BoardMembership.Role.ADMIN)
        col = Column.objects.create(board=self.board, name="C", position=0)
        swim = Swimlane.objects.create(board=self.board, name="S", position=0)
        self.card = Card.objects.create(board=self.board, column=col, swimlane=swim, title="x")
        self.client = APIClient(raise_request_exception=False)
        self.client.force_authenticate(self.user)
        b = self.board.id
        self.urls = {
            "archived": f"/api/v1/boards/{b}/cards/archived/",
            "timeline": f"/api/v1/boards/{b}/cards/{self.card.id}/timeline/",
            "movements": f"/api/v1/boards/{b}/movements/",
        }

    def test_huge_offset_is_400_on_every_endpoint(self):
        for name, url in self.urls.items():
            r = self.client.get(url, {"offset": HUGE})
            self.assertEqual(r.status_code, 400, name)
            self.assertIn("offset", r.json(), name)

    def test_just_past_max_is_400_and_max_is_empty_page(self):
        for name, url in self.urls.items():
            self.assertEqual(self.client.get(url, {"offset": 2**62 + 1}).status_code, 400, name)
            self.assertEqual(self.client.get(url, {"offset": 2**62}).status_code, 200, name)

    def test_negative_offset_clamps_to_zero(self):
        for name, url in self.urls.items():
            r = self.client.get(url, {"offset": -5})
            self.assertEqual(r.status_code, 200, name)
        self.assertEqual(self.client.get(self.urls["movements"], {"offset": -5}).json()["offset"], 0)
        self.assertEqual(self.client.get(self.urls["archived"], {"offset": -HUGE}).json()["offset"], 0)

    def test_non_numeric_offset_unchanged(self):
        for name, url in self.urls.items():
            self.assertEqual(self.client.get(url, {"offset": "abc"}).status_code, 200, name)

    def test_valid_offset_unchanged(self):
        r = self.client.get(self.urls["movements"], {"offset": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["offset"], 3)
        self.assertEqual(self.client.get(self.urls["archived"], {"offset": 0}).json()["offset"], 0)
        self.assertEqual(self.client.get(self.urls["timeline"], {"offset": 1}).status_code, 200)

    def test_negative_timeline_limit_not_500(self):
        r = self.client.get(self.urls["timeline"], {"limit": -5})
        self.assertLess(r.status_code, 500)

    def test_non_member_still_denied_before_offset_validation(self):
        # The membership check must run first so a 400 never confirms a board exists.
        self.client.force_authenticate(self.outsider)
        for name in ("archived", "movements"):
            ok = self.client.get(self.urls[name]).status_code
            bad = self.client.get(self.urls[name], {"offset": HUGE}).status_code
            self.assertEqual(ok, bad, name)
            self.assertIn(ok, (403, 404), name)
        ok = self.client.get(self.urls["timeline"]).status_code
        self.assertEqual(ok, self.client.get(self.urls["timeline"], {"offset": HUGE}).status_code)


class OtherPaginatedListProbe(TestCase):
    """Other offset-paginated lists (DRF LimitOffsetPagination) must not 500 either."""

    def setUp(self):
        self.admin = User.objects.create_user(username="sa", password="pass", is_staff=True, is_superuser=True)
        self.client = APIClient(raise_request_exception=False)
        self.client.force_authenticate(self.admin)

    def test_huge_offset_and_limit_not_500(self):
        for url in ("/api/v1/boards/", "/api/v1/admin/users/"):
            for params in ({"offset": HUGE}, {"offset": -HUGE}, {"page_size": HUGE}):
                r = self.client.get(url, params)
                self.assertLess(r.status_code, 500, f"{url} {params}")
