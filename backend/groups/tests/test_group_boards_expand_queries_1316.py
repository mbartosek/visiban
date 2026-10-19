"""Query-count regression for GET /groups/{id}/boards/?expand=group (#1316).

The expanded group payload must not add per-board queries (fresh FK lookup of
``obj.group`` plus a ``Group.ancestors()`` walk per board).
"""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board
from groups.models import Group, GroupMembership


class GroupBoardsExpandQueryCountTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner1316", password="pass")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        root = Group.objects.create(name="Root", owner=self.owner)
        mid = Group.objects.create(name="Mid", owner=self.owner, parent=root)
        self.group = Group.objects.create(name="Leaf", owner=self.owner, parent=mid)
        GroupMembership.objects.create(
            group=self.group, user=self.owner, role=GroupMembership.Role.ADMIN
        )

    def _count(self):
        url = f"/api/v1/groups/{self.group.id}/boards/?expand=group"
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return len(ctx), r.json()

    def _add_boards(self, n, start):
        for i in range(n):
            Board.objects.create(name=f"B{start + i}", owner=self.owner, group=self.group)

    def test_query_count_constant_in_board_count(self):
        self._add_boards(2, 0)
        small, _ = self._count()
        self._add_boards(8, 2)
        large, data = self._count()
        self.assertEqual(len(data), 10)
        self.assertEqual(small, large)

    def test_expanded_group_includes_ancestors(self):
        self._add_boards(1, 0)
        _, data = self._count()
        detail = data[0]["group_detail"]
        self.assertEqual([a["name"] for a in detail["ancestors"]], ["Root", "Mid"])
        self.assertEqual(detail["parent_name"], "Mid")

    def test_root_group_has_empty_ancestors_and_constant_queries(self):
        root = Group.objects.get(name="Root")
        GroupMembership.objects.create(
            group=root, user=self.owner, role=GroupMembership.Role.MEMBER
        )
        url = f"/api/v1/groups/{root.id}/boards/?expand=group"

        def fetch():
            with CaptureQueriesContext(connection) as ctx:
                r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            return len(ctx), r.json()

        Board.objects.create(name="R0", owner=self.owner, group=root)
        small, _ = fetch()
        for i in range(1, 6):
            Board.objects.create(name=f"R{i}", owner=self.owner, group=root)
        large, data = fetch()
        self.assertEqual(small, large)
        self.assertEqual(len(data), 6)
        detail = data[0]["group_detail"]
        self.assertEqual(detail["ancestors"], [])
        self.assertIsNone(detail["parent_name"])
