"""Import re-fetches annotate counts via ``annotate_board_counts`` (#1765).

The JSON and CSV import paths re-fetched the new board with
``Count("memberships") + Count("cards")`` joined side by side (members x cards),
the shape #1723 removed from the board list. They now share the correlated
subquery helper; the serialized counts must be unchanged.
"""
import csv
import inspect
import io
import json
from unittest import mock

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership
from boards.views import import_export
from boards.views.import_export import BoardExportThrottle, BoardImportThrottle

_URL = "/api/v1/boards/import/"


def _file(content, name):
    f = io.BytesIO(content.encode("utf-8"))
    f.name = name
    return f


@mock.patch.object(BoardImportThrottle, "allow_request", return_value=True)
@mock.patch.object(BoardExportThrottle, "allow_request", return_value=True)
class ImportBoardCountsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="importer", password="pass")
        self.client.force_authenticate(self.user)

    def test_json_import_counts_live_and_archived_cards(self, *_):
        cards = [{"title": f"c{i}", "column": "To Do", "swimlane": "General"} for i in range(4)]
        cards += [
            {"title": f"a{i}", "column": "To Do", "swimlane": "General",
             "archived_at": "2024-01-01T00:00:00Z"}
            for i in range(2)
        ]
        data = {
            "name": "J", "columns": [{"name": "To Do", "position": 0}],
            "swimlanes": [{"name": "General", "position": 0}], "cards": cards,
        }
        r = self.client.post(_URL, {"file": _file(json.dumps(data), "b.json")}, format="multipart")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(
            (r.data["member_count"], r.data["card_count"], r.data["archived_card_count"]), (1, 4, 2)
        )
        self.assertIsInstance(r.data["card_count"], int)

    def test_csv_import_counts_cards(self, *_):
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["Title", "Column", "Swimlane"])
        for i in range(3):
            w.writerow([f"c{i}", "To Do", "Lane"])
        r = self.client.post(_URL, {"file": _file(out.getvalue(), "b.csv")}, format="multipart")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(
            (r.data["member_count"], r.data["card_count"], r.data["archived_card_count"]), (1, 3, 0)
        )

    def test_empty_import_counts_are_zero_not_null(self, *_):
        data = {"name": "E", "columns": [{"name": "To Do", "position": 0}],
                "swimlanes": [{"name": "General", "position": 0}], "cards": []}
        r = self.client.post(_URL, {"file": _file(json.dumps(data), "b.json")}, format="multipart")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual((r.data["card_count"], r.data["archived_card_count"]), (0, 0))

    def test_views_use_helper_for_board_refetch(self, *_):
        # Narrow guard: the Board re-fetch annotations must not come back as
        # joined Count()s (#1723/#1765). Behavior tests above and in
        # test_trello_import.py cover the values.
        src = inspect.getsource(import_export)
        self.assertNotIn('Count("memberships"', src)
        self.assertGreaterEqual(src.count("annotate_board_counts("), 3)

    def test_refetch_sql_has_no_join_on_memberships_or_cards(self, *_):
        from boards.views._helpers import annotate_board_counts

        board = Board.objects.create(name="x", owner=self.user)
        for i in range(2):
            u = User.objects.create_user(username=f"m{i}", password="x")
            BoardMembership.objects.create(board=board, user=u, role="member")
        sql = str(annotate_board_counts(Board.objects.filter(pk=board.pk)).query).lower()
        # Counts are correlated subqueries; the outer query joins nothing.
        self.assertNotIn(" join ", sql)
        self.assertEqual(sql.count("(select count(*)"), 3)
