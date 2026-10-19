"""Unauthenticated access rejection tests (#401).

Verify that requests without authentication (no token, no session) receive
401 on all core API endpoints (the WWW-Authenticate header is pinned in
accounts/tests/test_anonymous_401_contract.py).
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, Column, Swimlane


def _make_board():
    """Create a board with basic fixtures for URL construction."""
    owner = User.objects.create_user(username="owner", password="pass")
    board = Board.objects.create(name="Test Board", owner=owner)
    BoardMembership.objects.create(board=board, user=owner, role=BoardMembership.Role.ADMIN)
    col = Column.objects.create(board=board, name="Backlog", position=0, allow_card_creation=True)
    swim = Swimlane.objects.create(board=board, name="General", position=0)
    card = Card.objects.create(
        board=board, column=col, swimlane=swim,
        title="Test Card", created_by=owner, position=0,
    )
    return board, col, swim, card


class UnauthenticatedBoardAccessTests(TestCase):
    """Unauthenticated users must be rejected on board endpoints."""

    def setUp(self):
        self.board, self.col, self.swim, self.card = _make_board()
        self.client = APIClient()  # no authentication

    # -- Board endpoints --

    def test_board_list_rejected(self):
        resp = self.client.get("/api/v1/boards/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_board_create_rejected(self):
        resp = self.client.post("/api/v1/boards/", {"name": "Intruder"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_board_detail_rejected(self):
        resp = self.client.get(f"/api/v1/boards/{self.board.pk}/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- Card endpoints --

    def test_card_list_rejected(self):
        resp = self.client.get(f"/api/v1/boards/{self.board.pk}/cards/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_card_create_rejected(self):
        resp = self.client.post(
            f"/api/v1/boards/{self.board.pk}/cards/",
            {"title": "Intruder Card", "column": self.col.pk, "swimlane": self.swim.pk},
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_card_detail_rejected(self):
        resp = self.client.get(f"/api/v1/boards/{self.board.pk}/cards/{self.card.pk}/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- Column endpoints --

    def test_column_list_rejected(self):
        resp = self.client.get(f"/api/v1/boards/{self.board.pk}/columns/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_column_create_rejected(self):
        resp = self.client.post(
            f"/api/v1/boards/{self.board.pk}/columns/",
            {"name": "Intruder Column", "position": 99},
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- Swimlane endpoints --

    def test_swimlane_list_rejected(self):
        resp = self.client.get(f"/api/v1/boards/{self.board.pk}/swimlanes/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_swimlane_create_rejected(self):
        resp = self.client.post(
            f"/api/v1/boards/{self.board.pk}/swimlanes/",
            {"name": "Intruder Swimlane", "position": 99},
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- Notification endpoints --

    def test_notification_list_rejected(self):
        resp = self.client.get("/api/v1/notifications/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- User search endpoint --

    def test_user_search_rejected(self):
        resp = self.client.get("/api/v1/users/", {"q": "owner"})
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # -- OpenAPI schema endpoints --

    def test_schema_json_rejected(self):
        resp = self.client.get("/api/schema/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_swagger_ui_rejected(self):
        resp = self.client.get("/api/schema/swagger-ui/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_redoc_rejected(self):
        resp = self.client.get("/api/schema/redoc/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
