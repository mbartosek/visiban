"""Group count annotations are independent subqueries, not a join (#1766).

``_member_count`` / ``_board_count`` / ``_subgroup_count`` used to be three
``Count(..., distinct=True)`` over memberships, boards and subgroups joined side
by side (members x boards x subgroups rows per group). The query count stayed
flat, so the query-count guards could not see it.
"""
import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board
from groups.counts import annotate_group_counts
from groups.models import Group, GroupMembership


def _strip_subqueries(sql):
    outer = sql.lower()
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
    return outer


class GroupCountsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        # 4 members x 3 boards x 2 subgroups: the case the join inflated.
        self.big = Group.objects.create(name="big", owner=self.owner)
        GroupMembership.objects.create(group=self.big, user=self.owner, role="admin")
        for i in range(3):
            u = User.objects.create_user(username=f"m{i}", password="x")
            GroupMembership.objects.create(group=self.big, user=u, role="member")
        for i in range(3):
            Board.objects.create(name=f"b{i}", owner=self.owner, group=self.big)
        self.subs = [
            Group.objects.create(name=f"s{i}", owner=self.owner, parent=self.big)
            for i in range(2)
        ]
        for s in self.subs:
            GroupMembership.objects.create(group=s, user=self.owner, role="admin")
        self.empty = Group.objects.create(name="empty", owner=self.owner)

    def _counts(self, row):
        return (row["member_count"], row["board_count"], row["subgroup_count"])

    def test_list_counts_correct(self):
        r = self.client.get("/api/v1/groups/")
        self.assertEqual(r.status_code, 200)
        data = r.data["results"] if "results" in r.data else r.data
        got = {g["name"]: g for g in data}
        self.assertEqual(self._counts(got["big"]), (4, 3, 2))
        self.assertEqual(self._counts(got["s0"]), (1, 0, 0))

    def test_empty_group_counts_are_zero_ints(self):
        # Owner has no membership row on ``empty``; counts must be 0, not null.
        row = annotate_group_counts(Group.objects.filter(pk=self.empty.pk)).get()
        for v in (row._member_count, row._board_count, row._subgroup_count):
            self.assertEqual(v, 0)
            self.assertIsInstance(v, int)

    def test_retrieve_and_subgroups_counts_correct(self):
        r = self.client.get(f"/api/v1/groups/{self.big.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._counts(r.data), (4, 3, 2))
        r = self.client.get(f"/api/v1/groups/{self.big.pk}/subgroups/")
        self.assertEqual(r.status_code, 200)
        data = r.data["results"] if "results" in r.data else r.data
        self.assertEqual({g["name"]: self._counts(g) for g in data},
                         {"s0": (1, 0, 0), "s1": (1, 0, 0)})

    def test_starred_filter_does_not_inflate_counts(self):
        # The ?starred= filter joins favorites after the count annotations.
        self.client.post(f"/api/v1/groups/{self.big.pk}/star/")
        r = self.client.get("/api/v1/groups/?starred=true")
        data = r.data["results"] if "results" in r.data else r.data
        got = {g["name"]: g for g in data}
        self.assertEqual(self._counts(got["big"]), (4, 3, 2))

    def test_annotated_queryset_sql_has_no_count_join(self):
        # The helper in isolation; each call site has its own shape test below.
        qs = annotate_group_counts(Group.objects.all())
        with CaptureQueriesContext(connection) as ctx:
            list(qs)
        sql = ctx.captured_queries[0]["sql"]
        outer = _strip_subqueries(sql)
        for table in ('join "group_memberships"', 'join "boards"', 'join "groups"'):
            self.assertNotIn(table, outer)
        self.assertNotIn("group by", outer)
        self.assertEqual(sql.lower().count("count(*)"), 3)

    def test_list_sql_has_no_count_join(self):
        with CaptureQueriesContext(connection) as ctx:
            self.client.get("/api/v1/groups/")
        self._assert_one_count_query_without_join(ctx)

    # -- per-queryset SQL shape (every annotated site, #1766) -------------

    def _assert_one_count_query_without_join(self, ctx, expected=1):
        """Exactly ``expected`` captured queries carry the count annotations (so
        the assertion cannot pass vacuously), and none has a count-related join
        or GROUP BY outside the correlated subqueries. The select_related
        parent chain legitimately joins groups on parent_id; a subgroup-count
        join would be the reverse (groups.id = child.parent_id)."""
        sqls = [q["sql"] for q in ctx.captured_queries if '"_subgroup_count"' in q["sql"]]
        self.assertEqual(len(sqls), expected, [q["sql"][:200] for q in ctx.captured_queries])
        for sql in sqls:
            self.assertEqual(sql.lower().count("count(*)"), 3)
            outer = _strip_subqueries(sql)
            self.assertNotIn('join "group_memberships"', outer)
            self.assertNotIn('join "boards"', outer)
            self.assertIsNone(
                re.search(r'join "groups" \w* ?on \("groups"\."id" = ', outer), outer
            )
            self.assertNotIn("group by", outer)

    def test_retrieve_sql_has_no_count_join(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(f"/api/v1/groups/{self.big.pk}/")
        self.assertEqual(r.status_code, 200)
        self._assert_one_count_query_without_join(ctx)

    def test_subgroups_sql_has_no_count_join(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(f"/api/v1/groups/{self.big.pk}/subgroups/")
        self.assertEqual(r.status_code, 200)
        self._assert_one_count_query_without_join(ctx, expected=2)

    def test_join_endpoint_sql_has_no_count_join_and_counts_correct(self):
        from groups.models import GroupInviteLink

        _, raw = GroupInviteLink.generate(group=self.big, created_by=self.owner)
        joiner = User.objects.create_user(username="joiner", password="x")
        client = APIClient()
        client.force_authenticate(joiner)
        with CaptureQueriesContext(connection) as ctx:
            r = client.post(f"/api/v1/groups/join/{raw}/")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self._counts(r.data), (5, 3, 2))
        self._assert_one_count_query_without_join(ctx)

    def test_admin_broadcast_sql_has_no_count_join(self):
        from django.contrib import admin as dj_admin
        from groups.admin import GroupAdmin

        model_admin = GroupAdmin(Group, dj_admin.site)
        with CaptureQueriesContext(connection) as ctx:
            model_admin._broadcast_after_commit(self.big.pk, actor_id=self.owner.pk)
        self._assert_one_count_query_without_join(ctx)
