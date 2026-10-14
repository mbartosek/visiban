"""A container delete announces the container, not each card (#1615).

Deleting a column, swimlane or board cascades to every card in it, archived
cards included. The contract (docs/api/websockets.md, "Deleting a container
deletes its cards") is that the delete publishes exactly one
``column.deleted`` / ``swimlane.deleted`` / ``board.deleted`` frame, after
commit and to that board's group only, and records exactly one change-feed
row, with no per-card ``card.deleted``. Clients drop the container's cards on
receipt. These tests pin that contract on every delete path: the REST destroy
views (#1599) and the Django admin single and bulk deletes (#1603).

A user delete never reaches cards: the admin refuses to delete a board owner,
and the card foreign keys to a user are ``SET_NULL``. The last class pins that.
"""

from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.admin import VisibanUserAdmin
from accounts.models import User
from boards.admin import BoardAdmin, ColumnAdmin, SwimlaneAdmin
from boards.models import Board, BoardEvent, BoardMembership, Card, Column, Swimlane

BROADCAST = "boards.broadcast.broadcast_board_event"


def _request(user):
    request = RequestFactory().post("/admin/")
    request.user = user
    return request


class _CascadeFixture(TestCase):
    """A board with two columns and two swimlanes, and a card (one of them
    archived) in each cell, plus an unrelated board that must hear nothing."""

    def setUp(self):
        self.owner = User.objects.create_superuser(
            username="owner1615", password="p", email="o1615@example.com"
        )
        self.board = Board.objects.create(name="Cascade", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        self.col_a = Column.objects.create(board=self.board, name="A", position=0)
        self.col_b = Column.objects.create(board=self.board, name="B", position=1)
        self.lane_a = Swimlane.objects.create(board=self.board, name="L1", position=0)
        self.lane_b = Swimlane.objects.create(board=self.board, name="L2", position=1)
        self.cards = {}
        for col in (self.col_a, self.col_b):
            for lane in (self.lane_a, self.lane_b):
                self.cards[(col.pk, lane.pk)] = Card.objects.create(
                    board=self.board,
                    column=col,
                    swimlane=lane,
                    title=f"{col.name}/{lane.name}",
                    created_by=self.owner,
                )
        # An archived card cascades with its container too.
        archived = self.cards[(self.col_a.pk, self.lane_a.pk)]
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])

        self.other = Board.objects.create(name="Other", owner=self.owner)
        other_col = Column.objects.create(board=self.other, name="X", position=0)
        other_lane = Swimlane.objects.create(board=self.other, name="Y", position=0)
        self.other_card = Card.objects.create(
            board=self.other,
            column=other_col,
            swimlane=other_lane,
            title="untouched",
            created_by=self.owner,
        )

    def assert_one_container_event(self, mock_broadcast, board_id, event, payload):
        """Exactly one frame, for the container, to this board; one feed row."""
        self.assertEqual(
            [c.args[:3] for c in mock_broadcast.call_args_list],
            [(board_id, event, payload)],
        )
        self.assertEqual(
            list(
                BoardEvent.objects.filter(board_id=board_id).values_list(
                    "event", flat=True
                )
            ),
            [event],
        )
        self.assertFalse(BoardEvent.objects.filter(event="card.deleted").exists())
        # The unrelated board's card survives and its board heard nothing.
        self.assertTrue(Card.objects.filter(pk=self.other_card.pk).exists())
        self.assertFalse(BoardEvent.objects.filter(board_id=self.other.pk).exists())


class RestContainerDeleteEventTests(_CascadeFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _delete(self, url):
        with patch(BROADCAST) as mock_broadcast:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                response = self.client.delete(url)
                # Deferred: nothing is published until the transaction commits.
                mock_broadcast.assert_not_called()
            for callback in callbacks:
                callback()
        self.assertEqual(response.status_code, 204, getattr(response, "data", None))
        return mock_broadcast

    def test_column_destroy_publishes_one_column_deleted_for_its_cards(self):
        uid = self.col_a.uid
        mock_broadcast = self._delete(
            f"/api/v1/boards/{self.board.pk}/columns/{self.col_a.pk}/"
        )
        self.assertFalse(Card.objects.filter(column_id=self.col_a.pk).exists())
        self.assertEqual(Card.objects.filter(board=self.board).count(), 2)
        self.assert_one_container_event(
            mock_broadcast, self.board.pk, "column.deleted", {"column_uid": uid}
        )

    def test_swimlane_destroy_publishes_one_swimlane_deleted_for_its_cards(self):
        uid = self.lane_a.uid
        mock_broadcast = self._delete(
            f"/api/v1/boards/{self.board.pk}/swimlanes/{self.lane_a.pk}/"
        )
        self.assertFalse(Card.objects.filter(swimlane_id=self.lane_a.pk).exists())
        self.assertEqual(Card.objects.filter(board=self.board).count(), 2)
        self.assert_one_container_event(
            mock_broadcast, self.board.pk, "swimlane.deleted", {"swimlane_uid": uid}
        )

    def test_board_destroy_publishes_one_board_deleted_for_its_cards(self):
        uid, board_id = self.board.uid, self.board.pk
        mock_broadcast = self._delete(f"/api/v1/boards/{board_id}/")
        self.assertFalse(Card.objects.filter(board_id=board_id).exists())
        self.assert_one_container_event(
            mock_broadcast, board_id, "board.deleted", {"board_uid": uid}
        )


class AdminContainerDeleteEventTests(_CascadeFixture):
    def setUp(self):
        super().setUp()
        self.request = _request(self.owner)

    def _run(self, fn, *args):
        with patch(BROADCAST) as mock_broadcast:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                fn(self.request, *args)
                mock_broadcast.assert_not_called()
            for callback in callbacks:
                callback()
        return mock_broadcast

    def test_column_delete_model(self):
        uid = self.col_a.uid
        mock_broadcast = self._run(
            ColumnAdmin(Column, AdminSite()).delete_model, self.col_a
        )
        self.assertFalse(Card.objects.filter(column_id=self.col_a.pk).exists())
        self.assert_one_container_event(
            mock_broadcast, self.board.pk, "column.deleted", {"column_uid": uid}
        )

    def test_column_delete_queryset(self):
        uid = self.col_a.uid
        mock_broadcast = self._run(
            ColumnAdmin(Column, AdminSite()).delete_queryset,
            Column.objects.filter(pk=self.col_a.pk),
        )
        self.assertFalse(Card.objects.filter(column_id=self.col_a.pk).exists())
        self.assert_one_container_event(
            mock_broadcast, self.board.pk, "column.deleted", {"column_uid": uid}
        )

    def test_swimlane_delete_model(self):
        uid = self.lane_a.uid
        mock_broadcast = self._run(
            SwimlaneAdmin(Swimlane, AdminSite()).delete_model, self.lane_a
        )
        self.assertFalse(Card.objects.filter(swimlane_id=self.lane_a.pk).exists())
        self.assert_one_container_event(
            mock_broadcast, self.board.pk, "swimlane.deleted", {"swimlane_uid": uid}
        )

    def test_swimlane_delete_queryset(self):
        uid = self.lane_a.uid
        mock_broadcast = self._run(
            SwimlaneAdmin(Swimlane, AdminSite()).delete_queryset,
            Swimlane.objects.filter(pk=self.lane_a.pk),
        )
        self.assertFalse(Card.objects.filter(swimlane_id=self.lane_a.pk).exists())
        self.assert_one_container_event(
            mock_broadcast, self.board.pk, "swimlane.deleted", {"swimlane_uid": uid}
        )

    def test_board_delete_model(self):
        uid, board_id = self.board.uid, self.board.pk
        mock_broadcast = self._run(
            BoardAdmin(Board, AdminSite()).delete_model, self.board
        )
        self.assertFalse(Card.objects.filter(board_id=board_id).exists())
        self.assert_one_container_event(
            mock_broadcast, board_id, "board.deleted", {"board_uid": uid}
        )

    def test_board_delete_queryset(self):
        uid, board_id = self.board.uid, self.board.pk
        mock_broadcast = self._run(
            BoardAdmin(Board, AdminSite()).delete_queryset,
            Board.objects.filter(pk=board_id),
        )
        self.assertFalse(Card.objects.filter(board_id=board_id).exists())
        self.assert_one_container_event(
            mock_broadcast, board_id, "board.deleted", {"board_uid": uid}
        )


class AdminUserDeleteKeepsCardsTests(_CascadeFixture):
    """A user delete is not a card cascade: the cards a deleted user created
    or was assigned stay, with the reference cleared."""

    def test_deleting_a_card_author_and_assignee_keeps_their_cards(self):
        author = User.objects.create_user(username="author1615", password="p")
        card = self.cards[(self.col_b.pk, self.lane_b.pk)]
        card.created_by = author
        card.assignee = author
        card.save(update_fields=["created_by", "assignee"])
        user_admin = VisibanUserAdmin(User, AdminSite())
        with patch(BROADCAST) as mock_broadcast:
            with self.captureOnCommitCallbacks(execute=True):
                user_admin.delete_model(_request(self.owner), author)
        card.refresh_from_db()
        self.assertIsNone(card.created_by_id)
        self.assertIsNone(card.assignee_id)
        self.assertEqual(Card.objects.filter(board=self.board).count(), 4)
        self.assertNotIn(
            "card.deleted", [c.args[1] for c in mock_broadcast.call_args_list]
        )
        self.assertFalse(BoardEvent.objects.filter(event="card.deleted").exists())
