"""Board list count annotations are independent subqueries, not a join (#1723).

``member_count`` / ``card_count`` / ``archived_card_count`` used to be three
``Count(..., distinct=True)`` over ``memberships`` and ``cards`` joined side by
side, so each board produced members x cards intermediate rows. The query count
stayed flat, so the query-count guards could not see it.
"""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, Column, Swimlane


def _board(owner, name, members, cards, archived):
    board = Board.objects.create(name=name, owner=owner)
    BoardMembership.objects.create(board=board, user=owner, role="admin")
    for i in range(members - 1):
        u = User.objects.create_user(username=f"{name}-m{i}", password="x")
        BoardMembership.objects.create(board=board, user=u, role="member")
    col = Column.objects.create(board=board, name="C", position=0)
    lane = Swimlane.objects.create(board=board, name="L", position=0)
    for n in range(cards + archived):
        Card.objects.create(
            board=board, column=col, swimlane=lane, title=f"c{n}", created_by=owner, position=n,
            archived_at=timezone.now() if n >= cards else None,
        )
    return board


class BoardListCountsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _list(self):
        r = self.client.get("/api/v1/boards/")
        self.assertEqual(r.status_code, 200)
        data = r.data["results"] if isinstance(r.data, dict) and "results" in r.data else r.data
        return {b["name"]: b for b in data}

    def test_counts_correct_with_many_members_and_cards(self):
        # 4 members x 6 live + 3 archived cards: the case the join inflated.
        _board(self.user, "big", members=4, cards=6, archived=3)
        _board(self.user, "other", members=2, cards=1, archived=0)
        got = self._list()
        self.assertEqual(
            (got["big"]["member_count"], got["big"]["card_count"], got["big"]["archived_card_count"]),
            (4, 6, 3),
        )
        self.assertEqual(
            (got["other"]["member_count"], got["other"]["card_count"], got["other"]["archived_card_count"]),
            (2, 1, 0),
        )

    def test_empty_board_counts_are_zero_ints_not_null(self):
        board = Board.objects.create(name="empty", owner=self.user)
        got = self._list()["empty"]
        self.assertEqual(got["card_count"], 0)
        self.assertEqual(got["archived_card_count"], 0)
        self.assertIsInstance(got["card_count"], int)
        self.assertIsInstance(got["member_count"], int)
        self.assertIsNotNone(board.pk)

    def test_list_sql_does_not_join_memberships_and_cards(self):
        _board(self.user, "big", members=3, cards=3, archived=1)
        with CaptureQueriesContext(connection) as ctx:
            self._list()
        board_sql = [
            q["sql"].lower() for q in ctx.captured_queries
            if q["sql"].startswith("SELECT DISTINCT") and "_card_count" in q["sql"]
        ]
        self.assertTrue(board_sql, [q["sql"][:400] for q in ctx.captured_queries])
        sql = board_sql[0]
        # Counts are correlated subqueries (one COUNT per figure); the outer
        # query must neither join the cards table nor aggregate over it.
        outer = sql
        while "(select" in outer:
            start = outer.index("(select")
            depth, i = 0, start
            while i < len(outer):
                depth += outer[i] == "("
                depth -= outer[i] == ")"
                i += 1
                if depth == 0:
                    break
            outer = outer[:start] + "<sub>" + outer[i:]
        self.assertNotIn('join "cards"', outer)
        self.assertNotIn("group by", outer)
        self.assertGreaterEqual(sql.count("count("), 3)
