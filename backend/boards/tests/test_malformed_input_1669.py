"""Malformed ids and non-object JSON bodies are 4xx, never 500 (#1669).

Same class as #1136. Body/id-type and out-of-range-id cases previously raised ValueError/
AttributeError/TypeError/OverflowError out of a members-only endpoint. The
non-numeric path-id route tests and the "still works" controls already passed
before the fix; they are regression guards, not proof of the fix.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Column, Swimlane, Card
from groups.models import Group, GroupMembership, GroupLabel

# A null body parses to an empty mapping in DRF, so it was never a 500.
HUGE = 10**30
NON_OBJECT_BODIES = ([1, 2], 5, "text", True)


class MalformedInputTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", password="pass")
        self.board = Board.objects.create(name="B", owner=self.admin)
        BoardMembership.objects.create(board=self.board, user=self.admin, role=BoardMembership.Role.ADMIN)
        self.col = Column.objects.create(board=self.board, name="C", position=0)
        self.swim = Swimlane.objects.create(board=self.board, name="S", position=0)
        self.card = Card.objects.create(board=self.board, column=self.col, swimlane=self.swim, title="x")
        self.group = Group.objects.create(name="G", owner=self.admin)
        GroupMembership.objects.create(group=self.group, user=self.admin, role=GroupMembership.Role.ADMIN)
        self.client = APIClient(raise_request_exception=False)
        self.client.force_authenticate(self.admin)

    def _assert_4xx(self, r, msg=""):
        self.assertTrue(400 <= r.status_code < 500, f"{msg}: got {r.status_code}")

    # --- path ids ---
    def test_group_member_non_numeric_id(self):
        for m in ("patch", "delete"):
            r = getattr(self.client, m)(f"/api/v1/groups/{self.group.id}/members/abc/", {}, format="json")
            self.assertEqual(r.status_code, 404, m)

    def test_group_label_non_numeric_id(self):
        for m in ("patch", "delete"):
            r = getattr(self.client, m)(f"/api/v1/groups/{self.group.id}/labels/abc/", {}, format="json")
            self.assertEqual(r.status_code, 404, m)

    def test_group_numeric_ids_still_route(self):
        label = GroupLabel.objects.create(group=self.group, name="L", color="#ff0000")
        r = self.client.patch(
            f"/api/v1/groups/{self.group.id}/labels/{label.id}/", {"name": "L2"}, format="json"
        )
        self.assertEqual(r.status_code, 200)

    def test_card_relation_non_numeric_id(self):
        r = self.client.delete(
            f"/api/v1/boards/{self.board.id}/cards/{self.card.id}/relations/abc/"
        )
        self.assertEqual(r.status_code, 404)

    # --- body fields ---
    def test_board_members_non_int_user_id(self):
        for bad in ("abc", [1], {"a": 1}, 1.5, True):
            r = self.client.post(
                f"/api/v1/boards/{self.board.id}/members/", {"user_id": bad}, format="json"
            )
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_board_members_non_object_body(self):
        for bad in NON_OBJECT_BODIES:
            r = self.client.post(f"/api/v1/boards/{self.board.id}/members/", bad, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_board_members_missing_user_id_still_404(self):
        r = self.client.post(f"/api/v1/boards/{self.board.id}/members/", {}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_columns_reorder_non_object_body(self):
        for bad in NON_OBJECT_BODIES:
            r = self.client.post(f"/api/v1/boards/{self.board.id}/columns/reorder/", bad, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_swimlanes_reorder_non_object_body(self):
        for bad in NON_OBJECT_BODIES:
            r = self.client.post(f"/api/v1/boards/{self.board.id}/swimlanes/reorder/", bad, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_reorder_valid_still_works(self):
        r = self.client.post(
            f"/api/v1/boards/{self.board.id}/columns/reorder/", {"order": [self.col.id]}, format="json"
        )
        self.assertEqual(r.status_code, 200)

    def test_notifications_mark_read_malformed(self):
        url = "/api/v1/notifications/mark-read/"
        for bad in NON_OBJECT_BODIES:
            self.assertEqual(self.client.post(url, bad, format="json").status_code, 400, repr(bad))
        for bad_ids in ("abc", 5, {"a": 1}, ["x"], [[1]], [None]):
            r = self.client.post(url, {"ids": bad_ids}, format="json")
            self.assertEqual(r.status_code, 400, repr(bad_ids))

    def test_notifications_mark_read_valid_still_works(self):
        url = "/api/v1/notifications/mark-read/"
        self.assertEqual(self.client.post(url, {"ids": [1, "2"]}, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, {"all": True}, format="json").status_code, 200)

    # --- ids beyond the database integer range ---
    def test_huge_path_ids_404(self):
        g = f"/api/v1/groups/{self.group.id}"
        for m in ("patch", "delete"):
            for tail in (f"members/{HUGE}/", f"labels/{HUGE}/"):
                r = getattr(self.client, m)(f"{g}/{tail}", {}, format="json")
                self.assertEqual(r.status_code, 404, f"{m} {tail}")
        r = self.client.delete(f"/api/v1/boards/{self.board.id}/cards/{self.card.id}/relations/{HUGE}/")
        self.assertEqual(r.status_code, 404)

    def test_huge_ids_in_bodies_400(self):
        b = f"/api/v1/boards/{self.board.id}"
        for bad in (HUGE, str(HUGE)):
            self.assertEqual(
                self.client.post("/api/v1/notifications/mark-read/", {"ids": [bad]}, format="json").status_code, 400
            )
            self.assertEqual(
                self.client.post(f"{b}/columns/reorder/", {"order": [bad]}, format="json").status_code, 400
            )
            self.assertEqual(
                self.client.post(f"{b}/swimlanes/reorder/", {"order": [bad]}, format="json").status_code, 400
            )
            self.assertEqual(
                self.client.post(f"{b}/members/", {"user_id": bad}, format="json").status_code, 400
            )

    # --- sibling sites fixed in the follow-up commit ---
    def test_custom_field_reorders_malformed(self):
        for path in ("custom-fields", "swimlane-custom-fields"):
            url = f"/api/v1/boards/{self.board.id}/{path}/reorder/"
            for method in (self.client.post, self.client.put):
                for bad in NON_OBJECT_BODIES:
                    self.assertEqual(method(url, bad, format="json").status_code, 400, f"{path} {bad!r}")
                for bad in (HUGE, str(HUGE), "abc", [1]):
                    r = method(url, {"order": [bad]}, format="json")
                    self.assertEqual(r.status_code, 400, f"{path} {bad!r}")
            # Valid (empty) order is unchanged.
            self.assertEqual(self.client.post(url, {"order": []}, format="json").status_code, 200)

    def test_transfer_ownership_new_owner_id_malformed(self):
        url = f"/api/v1/groups/{self.group.id}/transfer-ownership/"
        for bad in ("abc", [1], {"a": 1}, HUGE, str(HUGE), True):
            r = self.client.post(url, {"new_owner_id": bad, "confirmation": "G"}, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))
        # Missing / unknown member keep the existing 400.
        r = self.client.post(url, {"confirmation": "G"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(url, {"new_owner_id": 999999, "confirmation": "G"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("already be a group member", r.data["detail"])

    def test_swimlane_set_collapsed_non_object_body(self):
        url = f"/api/v1/boards/{self.board.id}/swimlanes/{self.swim.id}/set-collapsed/"
        for bad in NON_OBJECT_BODIES:
            self.assertEqual(self.client.patch(url, bad, format="json").status_code, 400, repr(bad))
        self.assertEqual(self.client.patch(url, {"is_collapsed": True}, format="json").status_code, 200)

    def test_move_group_non_object_body(self):
        url = f"/api/v1/boards/{self.board.id}/move-group/"
        for bad in NON_OBJECT_BODIES:
            self.assertEqual(self.client.post(url, bad, format="json").status_code, 400, repr(bad))
        self.assertEqual(self.client.post(url, {"group_id": None}, format="json").status_code, 200)

    def test_group_member_patch_non_object_body(self):
        other = User.objects.create_user(username="oth", password="pass")
        GroupMembership.objects.create(group=self.group, user=other, role=GroupMembership.Role.MEMBER)
        url = f"/api/v1/groups/{self.group.id}/members/{other.id}/"
        for bad in NON_OBJECT_BODIES:
            self.assertEqual(self.client.patch(url, bad, format="json").status_code, 400, repr(bad))
        self.assertEqual(self.client.patch(url, {"role": "viewer"}, format="json").status_code, 200)
