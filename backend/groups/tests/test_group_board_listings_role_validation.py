"""Group board listings apply the same stored-role validation as board detail (#1769).

``get_board_role()`` treats a stored role outside the allowed set as no access:
an invalid explicit ``BoardMembership`` role blocks group inheritance, and an
invalid ``GroupMembership`` role grants nothing. The three listing surfaces
(``GET /groups/{id}/boards/``, ``descendant-boards/`` and
``descendant-boards/paged/``) must agree with the detail endpoint. The invalid
values are written with ``QuerySet.update()`` because no API path can store them.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership
from boards.permissions import get_board_role
from groups.models import Group, GroupMembership


class GroupBoardListingRoleValidationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.member = User.objects.create_user(username="member", password="pass")
        self.group = Group.objects.create(name="Root", owner=self.owner)
        self.child = Group.objects.create(name="Child", owner=self.owner, parent=self.group)
        GroupMembership.objects.create(group=self.group, user=self.owner, role="admin")
        self.gm = GroupMembership.objects.create(group=self.group, user=self.member, role="viewer")
        self.board = Board.objects.create(name="Root Board", owner=self.owner, group=self.group)
        self.child_board = Board.objects.create(name="Child Board", owner=self.owner, group=self.child)
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def _urls(self):
        gid = self.group.id
        return {
            "boards": f"/api/v1/groups/{gid}/boards/",
            "descendant": f"/api/v1/groups/{gid}/descendant-boards/",
            "paged": f"/api/v1/groups/{gid}/descendant-boards/paged/",
        }

    def _ids(self, surface):
        r = self.client.get(self._urls()[surface])
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        rows = body["results"] if surface == "paged" else body
        return {b["id"] for b in rows}

    def test_valid_roles_list_boards_on_all_surfaces(self):
        self.assertIn(self.board.id, self._ids("boards"))
        self.assertNotIn(self.child_board.id, self._ids("boards"))
        for surface in ("descendant", "paged"):
            self.assertEqual(self._ids(surface), {self.board.id, self.child_board.id})

    def test_invalid_explicit_board_role_hides_board_everywhere(self):
        BoardMembership.objects.create(board=self.board, user=self.member, role="viewer")
        BoardMembership.objects.filter(board=self.board, user=self.member).update(role="bogus")
        self.assertIsNone(get_board_role(self.member, Board.objects.get(pk=self.board.pk)))
        self.assertNotIn(self.board.id, self._ids("boards"))
        for surface in ("descendant", "paged"):
            ids = self._ids(surface)
            self.assertNotIn(self.board.id, ids)
            # An unaffected board in the same subtree is still listed.
            self.assertIn(self.child_board.id, ids)

    def test_invalid_explicit_role_on_inherited_board_in_subgroup(self):
        BoardMembership.objects.create(board=self.child_board, user=self.member, role="viewer")
        BoardMembership.objects.filter(board=self.child_board).update(role="bogus")
        for surface in ("descendant", "paged"):
            self.assertNotIn(self.child_board.id, self._ids(surface))

    def test_owner_with_invalid_explicit_role_still_listed(self):
        """The owner rung precedes the explicit-membership rung in the ladder."""
        own = Board.objects.create(name="Own", owner=self.member, group=self.group)
        BoardMembership.objects.create(board=own, user=self.member, role="admin")
        BoardMembership.objects.filter(board=own).update(role="bogus")
        self.assertEqual(get_board_role(self.member, Board.objects.get(pk=own.pk)), "admin")
        for surface in ("boards", "descendant", "paged"):
            self.assertIn(own.id, self._ids(surface))

    def test_invalid_only_group_role_lists_no_boards(self):
        # Since #1829 such a membership grants no group access either, so the
        # group itself is not visible and every listing answers 404 (before
        # it, the listings answered 200 with no boards).
        GroupMembership.objects.filter(pk=self.gm.pk).update(role="bogus")
        self.assertIsNone(get_board_role(self.member, Board.objects.get(pk=self.board.pk)))
        for surface, url in self._urls().items():
            with self.subTest(surface=surface):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_invalid_nearest_ancestor_role_falls_through_to_farther_valid_one(self):
        """The ladder skips an invalid group role and keeps walking up, so a
        valid membership on the root still grants the child board."""
        GroupMembership.objects.create(group=self.child, user=self.member, role="viewer")
        GroupMembership.objects.filter(group=self.child, user=self.member).update(role="bogus")
        self.assertEqual(
            get_board_role(self.member, Board.objects.get(pk=self.child_board.pk)),
            "viewer",
        )
        for surface in ("descendant", "paged"):
            self.assertIn(self.child_board.id, self._ids(surface))
        # The ``boards`` surface lists the child group's own boards.
        r = self.client.get(f"/api/v1/groups/{self.child.id}/boards/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn(self.child_board.id, {b["id"] for b in r.json()})

    def test_site_wide_access_user_lists_every_board(self):
        BoardMembership.objects.create(board=self.board, user=self.member, role="viewer")
        BoardMembership.objects.filter(board=self.board).update(role="bogus")
        User.objects.filter(pk=self.member.pk).update(can_access_all_content=True)
        self.member.refresh_from_db()
        self.client.force_authenticate(self.member)
        self.assertIn(self.board.id, self._ids("boards"))
        for surface in ("descendant", "paged"):
            self.assertEqual(self._ids(surface), {self.board.id, self.child_board.id})
