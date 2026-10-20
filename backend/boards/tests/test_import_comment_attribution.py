"""Imported comments are the importer's and keep the source name (#1702).

Matches imported history rows (#1675): a comment created by a JSON or Trello
import is attributed to the importing user, keeps its original timestamp, and
keeps the author name the source named as text in ``imported_from_username``.
The Trello opt-in ``add_matched_members`` affects board membership and
assignees only. The comments API, the JSON export and the MCP card resource
surface the field; a comment posted on the board carries ``null``.
"""

import io
import json
from pathlib import Path

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, CardComment

IMPORT_URL = "/api/v1/boards/import/"
TRELLO_URL = "/api/v1/boards/import/trello/"
TRELLO_FIXTURE = Path(__file__).parent / "fixtures" / "trello_export.json"


def _file(data, name="board.json"):
    f = io.BytesIO(json.dumps(data).encode("utf-8"))
    f.name = name
    return f


def _payload(*comments):
    return {
        "name": "Comments",
        "columns": [{"name": "To Do", "position": 0}],
        "swimlanes": [{"name": "General", "position": 0}],
        "labels": [],
        "cards": [
            {
                "title": "Card",
                "column": "To Do",
                "swimlane": "General",
                "position": 0,
                "comments": list(comments),
            }
        ],
    }


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.importer = User.objects.create_user(username="importer", password="x")
        # ada shares a board with the importer, so Trello matching finds her
        # and the JSON assignee lookup could. Comments must still not link
        # to her.
        self.ada = User.objects.create_user(username="ada", password="x")
        shared = Board.objects.create(name="Shared", owner=self.importer)
        for u in (self.importer, self.ada):
            BoardMembership.objects.create(board=shared, user=u, role=BoardMembership.Role.MEMBER)
        self.client = APIClient()
        self.client.force_authenticate(self.importer)

    def _post_json(self, data, **extra):
        cache.clear()
        return self.client.post(IMPORT_URL, {"file": _file(data), **extra}, format="multipart")

    def _import_json(self, data, **extra):
        resp = self._post_json(data, **extra)
        self.assertEqual(resp.status_code, 201, resp.content)
        return Board.objects.get(pk=resp.data["id"])

    def _import_trello(self, data=None, mapping=None):
        cache.clear()
        data = data if data is not None else json.loads(TRELLO_FIXTURE.read_text())
        payload = {"file": _file(data, "trello.json")}
        if mapping is not None:
            payload["mapping"] = json.dumps(mapping)
        resp = self.client.post(TRELLO_URL + "?confirm=true", payload, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Board.objects.get(pk=resp.data["board"]["id"])


class TrelloCommentAttributionTests(_Base):
    def test_matched_author_is_kept_as_text_with_add_matched_members(self):
        board = self._import_trello(mapping={"add_matched_members": True})
        # The opt-in still adds ada and assigns her card...
        self.assertTrue(BoardMembership.objects.filter(board=board, user=self.ada).exists())
        # ...but no imported comment is hers.
        comments = CardComment.objects.filter(card__board=board)
        self.assertFalse(comments.filter(author=self.ada).exists())
        rows = sorted(comments.values_list("author__username", "imported_from_username"))
        self.assertEqual(
            rows,
            [
                ("importer", "Ada Lovelace"),
                ("importer", "Grace Hopper"),
                ("importer", "Stranger Danger"),
            ],
        )
        # The original time is kept.
        ada_comment = comments.get(imported_from_username="Ada Lovelace")
        self.assertEqual(ada_comment.created_at.isoformat()[:19], "2024-02-01T10:00:00")

    def test_attribution_is_the_same_with_the_opt_in_off(self):
        board = self._import_trello()
        rows = sorted(
            CardComment.objects.filter(card__board=board).values_list(
                "author__username", "imported_from_username", "body"
            )
        )
        self.assertEqual(
            rows,
            [
                ("importer", "Ada Lovelace", "(imported from Trello)\n\nLooks good to me"),
                ("importer", "Grace Hopper", "(imported from Trello)\n\nOn it"),
                ("importer", "Stranger Danger", "(imported from Trello)\n\nCan I help?"),
            ],
        )

    def test_long_author_name_is_cut_to_the_column(self):
        data = json.loads(TRELLO_FIXTURE.read_text())
        data["members"][2]["fullName"] = "S" * 400
        board = self._import_trello(data)
        comment = CardComment.objects.get(card__board=board, body__endswith="Can I help?")
        self.assertEqual(comment.imported_from_username, "S" * 150)

    def test_importer_owns_imported_comments_and_matched_member_does_not(self):
        board = self._import_trello(mapping={"add_matched_members": True})
        comment = CardComment.objects.get(card__board=board, imported_from_username="Ada Lovelace")
        url = f"/api/v1/boards/{board.id}/cards/{comment.card_id}/comments/{comment.id}/"
        ada_client = APIClient()
        ada_client.force_authenticate(self.ada)
        self.assertEqual(ada_client.delete(url).status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_json_round_trip_keeps_the_trello_name(self):
        board = self._import_trello()
        cache.clear()
        exported = json.loads(self.client.get(f"/api/v1/boards/{board.id}/export/?format=json").content)
        exported_comments = [c for card in exported["cards"] for c in card["comments"]]
        self.assertIn(
            {"author": "importer", "imported_from_username": "Ada Lovelace"},
            [{"author": c["author"], "imported_from_username": c["imported_from_username"]} for c in exported_comments],
        )
        reimported = self._import_json(exported)
        self.assertEqual(
            sorted(CardComment.objects.filter(card__board=reimported).values_list("imported_from_username", flat=True)),
            ["Ada Lovelace", "Grace Hopper", "Stranger Danger"],
        )


class JsonCommentAttributionTests(_Base):
    def test_comment_is_the_importers_with_source_author_and_time(self):
        board = self._import_json(
            _payload({"author": "ada", "body": "Hi", "created_at": "2024-03-04T05:06:07Z"})
        )
        comment = CardComment.objects.get(card__board=board)
        self.assertEqual((comment.author, comment.imported_from_username), (self.importer, "ada"))
        self.assertEqual(comment.created_at.isoformat(), "2024-03-04T05:06:07+00:00")

    def test_explicit_imported_from_username_wins_over_author(self):
        board = self._import_json(
            _payload({"author": "importer", "imported_from_username": "carol", "body": "Hi"})
        )
        self.assertEqual(CardComment.objects.get(card__board=board).imported_from_username, "carol")

    def test_comment_without_a_usable_author_is_still_marked_imported(self):
        # A non-string author was never read and is still not refused.
        board = self._import_json(
            _payload({"body": "a"}, {"author": None, "body": "b"}, {"author": 5, "body": "c"})
        )
        self.assertEqual(
            list(CardComment.objects.filter(card__board=board).values_list("imported_from_username", flat=True)),
            ["", "", ""],
        )

    def test_long_author_is_cut_and_nul_dropped(self):
        board = self._import_json(_payload({"author": "a\x00" + "b" * 200, "body": "Hi"}))
        self.assertEqual(CardComment.objects.get(card__board=board).imported_from_username, "a" + "b" * 149)

    def test_comments_off_writes_no_comments(self):
        board = self._import_json(_payload({"author": "ada", "body": "Hi"}), options=json.dumps({"comments": False}))
        self.assertFalse(CardComment.objects.filter(card__board=board).exists())

    def test_invalid_imported_from_username_is_refused(self):
        for value, fragment in (
            (["x"], "must be a string"),
            ({"a": 1}, "must be a string"),
            ("x" * 151, "at most 150 characters"),
            ("a\x00b", "NUL"),
        ):
            with self.subTest(value=str(value)[:20]):
                resp = self._post_json(_payload({"body": "Hi", "imported_from_username": value}))
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("imported_from_username", resp.data["detail"])
                self.assertIn(fragment, resp.data["detail"])
        self.assertFalse(Board.objects.filter(name="Comments").exists())


class CommentSurfacingTests(_Base):
    def setUp(self):
        super().setUp()
        self.board = self._import_json(_payload({"author": "ada", "body": "Imported"}))
        self.card = Card.objects.get(board=self.board)
        self.url = f"/api/v1/boards/{self.board.id}/cards/{self.card.id}/comments/"

    def test_comments_endpoint_and_posted_comment(self):
        # The field is read-only: a client cannot set it on a new comment.
        resp = self.client.post(self.url, {"body": "Posted", "imported_from_username": "mallory"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.data["imported_from_username"])
        listed = {c["body"]: c for c in self.client.get(self.url).json()}
        self.assertEqual(listed["Imported"]["imported_from_username"], "ada")
        self.assertEqual(listed["Imported"]["author"]["username"], "importer")
        self.assertIsNone(listed["Posted"]["imported_from_username"])

    def test_json_export_carries_the_field(self):
        CardComment.objects.create(card=self.card, author=self.importer, body="Posted")
        cache.clear()
        out = json.loads(self.client.get(f"/api/v1/boards/{self.board.id}/export/?format=json").content)
        self.assertEqual(
            {c["body"]: c["imported_from_username"] for c in out["cards"][0]["comments"]},
            {"Imported": "ada", "Posted": None},
        )

    def test_mcp_comment_shape(self):
        from mcp_server.tools import _serialize_comment

        imported = CardComment.objects.get(card=self.card)
        posted = CardComment.objects.create(card=self.card, author=self.importer, body="Posted")
        self.assertEqual(_serialize_comment(imported)["imported_from_username"], "ada")
        self.assertIsNone(_serialize_comment(posted)["imported_from_username"])
