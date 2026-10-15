"""Board members endpoint validates the role against the stored role set (#1654)."""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership
import importlib

from boards.permissions import can_modify_others_content, get_board_role, get_board_roles


class MembersRoleValidationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.admin = User.objects.create_user(username="badmin", password="pass")
        self.target = User.objects.create_user(username="target", password="pass")
        self.board = Board.objects.create(name="B", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.admin, role=BoardMembership.Role.ADMIN
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f"/api/v1/boards/{self.board.id}/members/"

    def _post(self, payload):
        return self.client.post(self.url, payload, format="json")

    def test_members_rejects_unknown_role(self):
        # "site_admin" is a derived role and is never stored.
        for bad in ("bogus_role", "owner", "root", "site_admin"):
            r = self._post({"user_id": self.target.id, "role": bad})
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, bad)
            self.assertIn("detail", r.data)
        self.assertFalse(
            BoardMembership.objects.filter(board=self.board, user=self.target).exists()
        )

    def test_members_rejects_unknown_role_on_existing_membership(self):
        r = self._post({"user_id": self.admin.id, "role": "bogus_role"})
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        m = BoardMembership.objects.get(board=self.board, user=self.admin)
        self.assertEqual(m.role, BoardMembership.Role.ADMIN)

    def test_members_rejects_non_string_role(self):
        for bad in (1, ["admin"], {"a": 1}, True):
            r = self._post({"user_id": self.target.id, "role": bad})
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, repr(bad))
            self.assertIn("detail", r.data)

    def test_members_missing_role_defaults_to_member(self):
        r = self._post({"user_id": self.target.id})
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data["role"], BoardMembership.Role.MEMBER)

    def test_members_accepts_valid_role(self):
        r = self._post({"user_id": self.target.id, "role": "viewer"})
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)

    def test_stored_invalid_role_has_no_access(self):
        BoardMembership.objects.filter(board=self.board, user=self.admin).update(
            role="bogus_role"
        )
        board = Board.objects.get(pk=self.board.pk)
        self.assertIsNone(get_board_role(self.admin, board))

    def test_stored_invalid_role_never_yields_moderator_rights(self):
        BoardMembership.objects.filter(board=self.board, user=self.admin).update(
            role="bogus_role", is_moderator=True
        )
        board = Board.objects.get(pk=self.board.pk)
        role = get_board_role(self.admin, board)
        self.assertIsNone(role)
        self.assertFalse(can_modify_others_content(board, role, self.admin))
        self.assertFalse(getattr(board, "_cached_membership", None))

    def test_bulk_resolver_returns_none_for_stored_invalid_role(self):
        BoardMembership.objects.filter(board=self.board, user=self.admin).update(
            role="bogus_role"
        )
        board = Board.objects.get(pk=self.board.pk)
        self.assertIsNone(get_board_roles(self.admin, [board])[board.pk])

    def test_stored_invalid_group_role_is_ignored(self):
        from groups.models import Group, GroupMembership

        group = Group.objects.create(name="G", owner=self.owner)
        GroupMembership.objects.create(group=group, user=self.target, role="viewer")
        GroupMembership.objects.filter(group=group, user=self.target).update(
            role="bogus_role"
        )
        self.board.group = group
        self.board.save()
        board = Board.objects.get(pk=self.board.pk)
        self.assertIsNone(get_board_role(self.target, board))
        self.assertIsNone(get_board_roles(self.target, [board])[board.pk])


class NormalizeRoleMigrationTests(TestCase):
    def test_only_invalid_rows_change(self):
        mod = importlib.import_module(
            "boards.migrations.0071_normalize_board_membership_role"
        )
        owner = User.objects.create_user(username="o2", password="pass")
        board = Board.objects.create(name="B2", owner=owner)
        users = [
            User.objects.create_user(username=f"u{i}", password="pass") for i in range(3)
        ]
        BoardMembership.objects.create(board=board, user=users[0], role="admin", is_moderator=True)
        BoardMembership.objects.create(board=board, user=users[1], role="member", is_moderator=True)
        BoardMembership.objects.create(board=board, user=users[2], role="bogus_role", is_moderator=True)

        from django.apps import apps

        with self.assertLogs(mod.logger, level="WARNING") as cm:
            mod.normalize_roles(apps, None)
        self.assertIn(f"({board.id}, {users[2].id})", cm.output[0])

        def get(u):
            return BoardMembership.objects.get(board=board, user=u)

        self.assertEqual((get(users[0]).role, get(users[0]).is_moderator), ("admin", True))
        self.assertEqual((get(users[1]).role, get(users[1]).is_moderator), ("member", True))
        self.assertEqual((get(users[2]).role, get(users[2]).is_moderator), ("viewer", False))
