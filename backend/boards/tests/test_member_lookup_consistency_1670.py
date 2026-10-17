"""Member lookups match active users only and board routes 404 for non-members (#1670)."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership
from groups.models import Group, GroupMembership


class BoardMemberLookupTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="badmin", password="pass")
        self.active = User.objects.create_user(username="active", password="pass")
        self.inactive = User.objects.create_user(
            username="gone", password="pass", is_active=False
        )
        self.outsider = User.objects.create_user(username="outsider", password="pass")
        self.board = Board.objects.create(name="B", owner=self.admin)
        BoardMembership.objects.create(
            board=self.board, user=self.admin, role=BoardMembership.Role.ADMIN
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f"/api/v1/boards/{self.board.id}/members/"

    def test_active_user_can_be_added(self):
        r = self.client.post(self.url, {"user_id": self.active.id}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_inactive_user_is_not_added(self):
        r = self.client.post(self.url, {"user_id": self.inactive.id}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(
            BoardMembership.objects.filter(board=self.board, user=self.inactive).exists()
        )

    def test_inactive_and_unknown_ids_get_the_same_response(self):
        unknown = self.client.post(self.url, {"user_id": 987654}, format="json")
        inactive = self.client.post(self.url, {"user_id": self.inactive.id}, format="json")
        self.assertEqual(unknown.status_code, inactive.status_code)
        self.assertEqual(unknown.data, inactive.data)

    def test_inactive_member_can_still_be_removed(self):
        BoardMembership.objects.create(
            board=self.board, user=self.inactive, role=BoardMembership.Role.MEMBER
        )
        r = self.client.delete(f"{self.url}{self.inactive.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(
            BoardMembership.objects.filter(board=self.board, user=self.inactive).exists()
        )

    def test_role_of_existing_inactive_member_can_be_changed(self):
        BoardMembership.objects.create(
            board=self.board, user=self.inactive, role=BoardMembership.Role.MEMBER
        )
        r = self.client.post(
            self.url, {"user_id": self.inactive.id, "role": "viewer"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            BoardMembership.objects.get(board=self.board, user=self.inactive).role, "viewer"
        )

    def test_removal_without_membership_is_one_404_for_every_kind_of_id(self):
        responses = [
            self.client.delete(f"{self.url}{uid}/")
            for uid in (987654, self.inactive.id, self.active.id)
        ]
        for r in responses:
            self.assertEqual(r.status_code, 404)
        self.assertEqual(responses[0].data, responses[1].data)
        self.assertEqual(responses[0].data, responses[2].data)


class BoardScopedNotFoundTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.outsider = User.objects.create_user(username="outsider", password="pass")
        self.viewer = User.objects.create_user(username="viewer", password="pass")
        self.board = Board.objects.create(name="B", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.viewer, role=BoardMembership.Role.VIEWER
        )
        self.client = APIClient()

    def test_non_member_gets_404_on_nested_routes(self):
        self.client.force_authenticate(self.outsider)
        base = f"/api/v1/boards/{self.board.id}"
        self.assertEqual(self.client.get(f"{base}/full/").status_code, 404)
        r = self.client.post(f"{base}/members/", {"user_id": self.viewer.id}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.delete(f"{base}/members/{self.viewer.id}/").status_code, 404)

    def test_existing_and_missing_boards_answer_alike_for_non_members(self):
        self.client.force_authenticate(self.outsider)
        existing = self.client.post(
            f"/api/v1/boards/{self.board.id}/members/", {"user_id": 1}, format="json"
        )
        missing = self.client.post("/api/v1/boards/999999/members/", {"user_id": 1}, format="json")
        self.assertEqual(existing.status_code, missing.status_code)
        self.assertEqual(existing.data, missing.data)

    def test_insufficient_role_on_visible_board_is_still_403(self):
        self.client.force_authenticate(self.viewer)
        r = self.client.post(
            f"/api/v1/boards/{self.board.id}/members/", {"user_id": self.outsider.id}, format="json"
        )
        self.assertEqual(r.status_code, 403)


class GroupMemberLookupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="gowner", password="pass")
        self.inactive = User.objects.create_user(
            username="gone", password="pass", is_active=False
        )
        self.group = Group.objects.create(name="G", owner=self.owner)
        GroupMembership.objects.create(
            group=self.group, user=self.owner, role=GroupMembership.Role.ADMIN
        )
        GroupMembership.objects.create(
            group=self.group, user=self.inactive, role=GroupMembership.Role.MEMBER
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _url(self, uid):
        return f"/api/v1/groups/{self.group.id}/members/{uid}/"

    def test_role_change_without_membership_is_one_404_for_every_kind_of_id(self):
        active_non_member = User.objects.create_user(username="plain", password="pass")
        new_inactive = User.objects.create_user(username="gone2", password="pass", is_active=False)
        responses = [
            self.client.patch(self._url(uid), {"role": "viewer"}, format="json")
            for uid in (987654, new_inactive.id, active_non_member.id)
        ]
        for r in responses:
            self.assertEqual(r.status_code, 404)
        self.assertEqual(responses[0].data, responses[1].data)
        self.assertEqual(responses[0].data, responses[2].data)

    def test_role_of_existing_inactive_member_can_be_changed(self):
        r = self.client.patch(self._url(self.inactive.id), {"role": "viewer"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            GroupMembership.objects.get(group=self.group, user=self.inactive).role, "viewer"
        )

    def test_removal_without_membership_is_one_404_for_every_kind_of_id(self):
        active_non_member = User.objects.create_user(username="plain", password="pass")
        new_inactive = User.objects.create_user(username="gone2", password="pass", is_active=False)
        responses = [
            self.client.delete(self._url(uid))
            for uid in (987654, new_inactive.id, active_non_member.id)
        ]
        for r in responses:
            self.assertEqual(r.status_code, 404)
        self.assertEqual(responses[0].data, responses[1].data)
        self.assertEqual(responses[0].data, responses[2].data)

    def test_inactive_member_can_still_be_removed(self):
        r = self.client.delete(self._url(self.inactive.id))
        self.assertEqual(r.status_code, 204)
        self.assertFalse(
            GroupMembership.objects.filter(group=self.group, user=self.inactive).exists()
        )

    def test_non_admin_member_cannot_patch_a_member(self):
        plain = User.objects.create_user(username="plainmember", password="pass")
        GroupMembership.objects.create(group=self.group, user=plain, role=GroupMembership.Role.MEMBER)
        self.client.force_authenticate(plain)
        r = self.client.patch(self._url(self.inactive.id), {"role": "viewer"}, format="json")
        self.assertEqual(r.status_code, 403)


class NestedRouteAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner2", password="pass")
        self.target = User.objects.create_user(username="target2", password="pass")
        self.board = Board.objects.create(name="B", owner=self.owner)
        self.url = f"/api/v1/boards/{self.board.id}/members/"

    def test_anonymous_caller_is_unauthorized(self):
        r = APIClient().post(self.url, {"user_id": self.target.id}, format="json")
        self.assertIn(r.status_code, (401, 403))
        self.assertNotEqual(r.status_code, 404)

    def test_site_admin_without_all_content_access_gets_404_on_a_board_they_cannot_see(self):
        sa = User.objects.create_user(username="sa2", password="pass", is_site_admin=True)
        c = APIClient()
        c.force_authenticate(sa)
        r = c.post(self.url, {"user_id": self.target.id}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_group_admin_inherits_board_admin_on_member_add(self):
        group = Group.objects.create(name="G2", owner=self.owner)
        gadmin = User.objects.create_user(username="gadmin", password="pass")
        GroupMembership.objects.create(group=group, user=gadmin, role=GroupMembership.Role.ADMIN)
        self.board.group = group
        self.board.save()
        c = APIClient()
        c.force_authenticate(gadmin)
        r = c.post(self.url, {"user_id": self.target.id}, format="json")
        self.assertEqual(r.status_code, 201)
