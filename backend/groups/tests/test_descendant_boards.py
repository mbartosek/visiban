"""Tests for GET /api/v1/groups/{id}/descendant-boards/ and its paged sibling (#1317).

Verifies that boards in groups nested 3+ levels deep are returned, that the
response is scoped to what the requesting user is allowed to see, and that
boards from the root group itself are included.
"""
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board
from groups.models import Group, GroupMembership


def _make_group(owner, name="Group", parent=None):
    group = Group.objects.create(name=name, owner=owner, parent=parent)
    GroupMembership.objects.create(group=group, user=owner, role=GroupMembership.Role.ADMIN)
    return group


def _make_board(owner, group, name="Board"):
    return Board.objects.create(name=name, owner=owner, group=group)


class DescendantBoardsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

        # Build a 3-level deep group hierarchy:
        # root → child → grandchild
        self.root = _make_group(self.owner, name="Root")
        self.child = _make_group(self.owner, name="Child", parent=self.root)
        self.grandchild = _make_group(self.owner, name="Grandchild", parent=self.child)

        self.root_board = _make_board(self.owner, self.root, name="Root Board")
        self.child_board = _make_board(self.owner, self.child, name="Child Board")
        self.grandchild_board = _make_board(self.owner, self.grandchild, name="Grandchild Board")

    def _get(self, group_id):
        return self.client.get(f"/api/v1/groups/{group_id}/descendant-boards/")

    def test_returns_boards_from_root_group(self):
        r = self._get(self.root.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        board_ids = [b["id"] for b in r.json()]
        self.assertIn(self.root_board.id, board_ids)

    def test_returns_boards_from_direct_child(self):
        r = self._get(self.root.id)
        board_ids = [b["id"] for b in r.json()]
        self.assertIn(self.child_board.id, board_ids)

    def test_returns_boards_from_deeply_nested_grandchild(self):
        """Board in a group 3 levels deep must appear in the descendant-boards response."""
        r = self._get(self.root.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        board_ids = [b["id"] for b in r.json()]
        self.assertIn(self.grandchild_board.id, board_ids)

    def test_scoped_to_subtree_not_all_groups(self):
        """Only descendants of the requested group are returned, not all boards."""
        # Create a sibling group (not a descendant of root)
        sibling = _make_group(self.owner, name="Sibling")
        sibling_board = _make_board(self.owner, sibling, name="Sibling Board")
        r = self._get(self.root.id)
        board_ids = [b["id"] for b in r.json()]
        self.assertNotIn(sibling_board.id, board_ids)

    def test_child_endpoint_returns_only_its_subtree(self):
        """Requesting the child group only returns boards in child and grandchild."""
        r = self._get(self.child.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        board_ids = [b["id"] for b in r.json()]
        self.assertIn(self.child_board.id, board_ids)
        self.assertIn(self.grandchild_board.id, board_ids)
        self.assertNotIn(self.root_board.id, board_ids)

    def test_requires_authentication(self):
        unauth_client = APIClient()
        r = unauth_client.get(f"/api/v1/groups/{self.root.id}/descendant-boards/")
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_member_cannot_access(self):
        self.client.force_authenticate(self.other)
        r = self._get(self.root.id)
        # Non-members get 404 because get_object() restricts to accessible group IDs.
        self.assertIn(r.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    def test_response_includes_group_name_field(self):
        """Each board in the response must include the group_name field."""
        r = self._get(self.root.id)
        boards = r.json()
        for b in boards:
            self.assertIn("group_name", b, f"Board {b.get('id')} missing group_name field")

    def test_four_levels_deep(self):
        """Board nested 4 levels deep (root → child → grandchild → great-grandchild) is included."""
        great_grandchild = _make_group(self.owner, name="GreatGrandchild", parent=self.grandchild)
        deep_board = _make_board(self.owner, great_grandchild, name="Deep Board")
        r = self._get(self.root.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        board_ids = [b["id"] for b in r.json()]
        self.assertIn(deep_board.id, board_ids)

    # ------------------------------------------------------------------
    # #845 — ancestors in group_detail for full relative breadcrumb
    # ------------------------------------------------------------------

    def test_group_detail_includes_ancestors_root_first(self):
        """Each board response includes group_detail.ancestors (root-first)."""
        r = self._get(self.root.id)
        boards = {b["id"]: b for b in r.json()}

        grandchild_payload = boards[self.grandchild_board.id]
        self.assertIsNotNone(grandchild_payload.get("group_detail"))
        ancestors = grandchild_payload["group_detail"]["ancestors"]
        # Root-first: [root, child]; the board's own group (grandchild) is NOT
        # in the ancestors list — it is the subject.
        self.assertEqual([a["name"] for a in ancestors], ["Root", "Child"])
        self.assertEqual([a["id"] for a in ancestors], [self.root.id, self.child.id])

    def test_group_detail_ancestors_empty_for_root_level_board(self):
        """A board whose group has no parent returns ancestors=[]."""
        # The root_board lives in self.root which has parent=None.
        r = self._get(self.root.id)
        boards = {b["id"]: b for b in r.json()}
        self.assertEqual(boards[self.root_board.id]["group_detail"]["ancestors"], [])

    def test_descendant_boards_ancestors_avoids_n_plus_1(self):
        """Resolving ancestors for many boards must not scale per-board.

        Uses 50 boards so any per-board FK lookup (ancestors walk or the
        parent_name join) would clearly exceed the constant-query budget.
        """
        for i in range(50):
            _make_board(self.owner, self.grandchild, name=f"Extra {i}")

        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        # Warm any first-request caches (session, user lookup).
        self._get(self.root.id)

        with CaptureQueriesContext(connection) as ctx:
            r = self._get(self.root.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        # With the bulk ancestor map (#845) and ``group__parent`` joined in
        # the boards queryset, query count is bounded regardless of how many
        # boards are returned. A per-board FK lookup would push this well
        # past the threshold at 50+ boards.
        self.assertLess(
            len(ctx.captured_queries),
            25,
            msg=f"Descendant boards endpoint issued {len(ctx.captured_queries)} "
                f"queries for 53 boards — ancestor / parent_name N+1 regression "
                f"suspected.",
        )


# ----------------------------------------------------------------------
# #1317 — GET /api/v1/groups/{id}/descendant-boards/paged/
# ----------------------------------------------------------------------

_PAGED = "/api/v1/groups/{}/descendant-boards/paged/"


class DescendantBoardsPagedTests(TestCase):
    """The paged sibling is a windowed view of the exact descendant-boards set.

    The bare-array endpoint is the 1.0 contract and must not change for any
    query string; the paged endpoint must page through the same boards with the
    same per-board payload, the same access gate, and a bounded query count.
    """

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

        self.root = _make_group(self.owner, name="Root")
        self.child = _make_group(self.owner, name="Child", parent=self.root)
        self.grandchild = _make_group(self.owner, name="Grandchild", parent=self.child)

        self.root_board = _make_board(self.owner, self.root, name="Root Board")
        self.child_board = _make_board(self.owner, self.child, name="Child Board")
        self.grandchild_board = _make_board(self.owner, self.grandchild, name="Grandchild Board")

    def _paged(self, group_id, **params):
        return self.client.get(_PAGED.format(group_id), params)

    def _walk(self, group_id, page_size):
        """Follow offset/page_size to the end; return every board payload seen."""
        seen, offset = [], 0
        while True:
            r = self._paged(group_id, page_size=page_size, offset=offset)
            self.assertEqual(r.status_code, status.HTTP_200_OK)
            body = r.json()
            seen.extend(body["results"])
            offset += body["page_size"]
            if offset >= body["count"]:
                return seen

    # -- the bare-array contract is untouched ---------------------------

    def test_default_endpoint_still_returns_every_board_as_a_bare_array(self):
        """More boards than the paginator's default page (50) and a page_size in
        the query string: descendant-boards/ still answers one full array."""
        for i in range(60):
            _make_board(self.owner, self.grandchild, name=f"Extra {i}")
        for params in ({}, {"page_size": 10, "offset": 5}):
            with self.subTest(params=params):
                r = self.client.get(f"/api/v1/groups/{self.root.id}/descendant-boards/", params)
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                body = r.json()
                self.assertIsInstance(body, list)
                self.assertEqual(len(body), 63)

    # -- envelope and paging --------------------------------------------

    def test_returns_the_standard_envelope_with_default_page_size(self):
        r = self._paged(self.root.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        self.assertEqual(set(body), {"count", "offset", "page_size", "results"})
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["page_size"], 50)
        self.assertEqual(len(body["results"]), 3)

    def test_pages_are_bounded_and_ordered_by_id(self):
        expected = sorted([self.root_board.id, self.child_board.id, self.grandchild_board.id])
        first = self._paged(self.root.id, page_size=2).json()
        self.assertEqual(first["count"], 3)
        self.assertEqual(first["page_size"], 2)
        self.assertEqual([b["id"] for b in first["results"]], expected[:2])
        second = self._paged(self.root.id, page_size=2, offset=2).json()
        self.assertEqual(second["count"], 3)
        self.assertEqual(second["offset"], 2)
        self.assertEqual([b["id"] for b in second["results"]], expected[2:])

    def test_walking_every_page_yields_exactly_the_full_list(self):
        """Same boards, no duplicates, and the same per-board payload (including
        the #845 ancestors chain) as the bare-array endpoint."""
        for i in range(7):
            _make_board(self.owner, self.child, name=f"Extra {i}")
        full = self.client.get(f"/api/v1/groups/{self.root.id}/descendant-boards/").json()
        walked = self._walk(self.root.id, page_size=3)
        self.assertEqual(len(walked), len(full))
        self.assertEqual(
            sorted(walked, key=lambda b: b["id"]),
            sorted(full, key=lambda b: b["id"]),
        )

    def test_offset_past_the_end_returns_an_empty_page(self):
        body = self._paged(self.root.id, offset=10).json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["results"], [])

    def test_invalid_params_fall_back_per_drf_limit_offset_rules(self):
        """DRF's LimitOffsetPagination: a non-positive or non-integer page_size
        means the default, an oversized one is clamped to max_limit, and a bad
        offset means 0 — never a 400 or a 500."""
        cases = [
            ({"page_size": "abc"}, 50, 0),
            ({"page_size": 0}, 50, 0),
            ({"page_size": -5}, 50, 0),
            ({"page_size": 100000}, 200, 0),
            ({"offset": "abc"}, 50, 0),
            ({"offset": -3}, 50, 0),
        ]
        for params, page_size, offset in cases:
            with self.subTest(params=params):
                r = self._paged(self.root.id, **params)
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                body = r.json()
                self.assertEqual(body["page_size"], page_size)
                self.assertEqual(body["offset"], offset)
                self.assertEqual(len(body["results"]), 3)

    # -- access ---------------------------------------------------------

    def test_requires_authentication(self):
        r = APIClient().get(_PAGED.format(self.root.id))
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_member_gets_the_same_denial_as_the_full_list(self):
        self.client.force_authenticate(self.other)
        full = self.client.get(f"/api/v1/groups/{self.root.id}/descendant-boards/")
        paged = self._paged(self.root.id)
        self.assertIn(paged.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])
        self.assertEqual(paged.status_code, full.status_code)

    def test_subgroup_member_pages_only_their_visible_subtree(self):
        """A member of ``child`` only: every page stays inside the boards the
        full list shows them, and the ancestor root's board never appears."""
        GroupMembership.objects.create(
            group=self.child, user=self.other, role=GroupMembership.Role.MEMBER,
        )
        _make_board(self.owner, self.root, name="Root Board 2")
        self.client.force_authenticate(self.other)

        full = self.client.get(f"/api/v1/groups/{self.child.id}/descendant-boards/").json()
        walked_ids = [b["id"] for b in self._walk(self.child.id, page_size=1)]
        self.assertEqual(sorted(walked_ids), sorted(b["id"] for b in full))
        self.assertEqual(
            set(walked_ids), {self.child_board.id, self.grandchild_board.id},
        )
        self.assertIn(
            self._paged(self.root.id).status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    # -- query count ----------------------------------------------------

    def test_query_count_is_constant_in_page_size(self):
        """53 boards: a page of 5 and a page of 50 issue the same number of
        queries, so the paged endpoint keeps descendant-boards' bounded shape
        (no per-board lookup) plus only the paginator's COUNT."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(50):
            _make_board(self.owner, self.grandchild, name=f"Extra {i}")
        self._paged(self.root.id, page_size=5)  # warm first-request caches

        counts = {}
        for page_size in (5, 50):
            with CaptureQueriesContext(connection) as ctx:
                r = self._paged(self.root.id, page_size=page_size)
            self.assertEqual(r.status_code, status.HTTP_200_OK)
            self.assertEqual(len(r.json()["results"]), page_size)
            counts[page_size] = len(ctx.captured_queries)
        self.assertEqual(counts[5], counts[50], msg=f"query counts by page size: {counts}")

        # And no more than the bare-array endpoint plus the paginator's COUNT,
        # so a few extra queries in the paged path alone are caught too.
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(f"/api/v1/groups/{self.root.id}/descendant-boards/")
        self.assertLessEqual(
            counts[50], len(ctx.captured_queries) + 1,
            msg=f"paged {counts}, bare {len(ctx.captured_queries)}",
        )


class DescendantBoardsSchemaTests(SimpleTestCase):
    """#1317: the bare-array operation keeps its published type; the paged
    sibling publishes the standard envelope and only the params it honors."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from drf_spectacular.generators import SchemaGenerator

        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def _op(self, path):
        return self.schema["paths"][path]["get"]

    def test_full_list_is_still_a_bare_board_array(self):
        op = self._op("/api/v1/groups/{id}/descendant-boards/")
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema.get("type"), "array")
        self.assertEqual(schema["items"]["$ref"], "#/components/schemas/Board")
        params = {p["name"] for p in op.get("parameters", [])}
        self.assertNotIn("page_size", params)
        self.assertNotIn("offset", params)

    def test_paged_returns_the_standard_envelope(self):
        op = self._op("/api/v1/groups/{id}/descendant-boards/paged/")
        schema = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["$ref"], "#/components/schemas/PaginatedBoardList")
        page = self.schema["components"]["schemas"]["PaginatedBoardList"]
        self.assertEqual(set(page["required"]), {"count", "offset", "page_size", "results"})
        self.assertEqual(page["properties"]["results"]["items"]["$ref"], "#/components/schemas/Board")
        params = {p["name"]: p for p in op.get("parameters", [])}
        self.assertEqual(set(params) - {"id"}, {"offset", "page_size"})
        self.assertFalse(params["offset"].get("required", False))
        self.assertFalse(params["page_size"].get("required", False))
