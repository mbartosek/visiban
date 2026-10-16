"""Changing a group's ``parent`` through PATCH/PUT /api/v1/groups/{id}/ (#1657).

Pins the rules in ``GroupSerializer.validate_parent``: admin on the new parent,
who may make a group top-level, no self or subgroup as parent, and the
``_GROUP_TRAVERSAL_MAX_DEPTH`` bound on the resulting tree.
"""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from groups.models import Group, GroupMembership, _GROUP_TRAVERSAL_MAX_DEPTH

ADMIN = GroupMembership.Role.ADMIN


class GroupParentChangeTests(TestCase):
    def setUp(self):
        self.root_admin = User.objects.create_user(username="rootadmin", password="pw")
        self.group_owner = User.objects.create_user(username="gowner", password="pw")
        self.group_admin = User.objects.create_user(username="gadmin", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")

        self.root = Group.objects.create(name="Root", owner=self.root_admin)
        GroupMembership.objects.create(group=self.root, user=self.root_admin, role=ADMIN)
        self.group = Group.objects.create(name="Team", owner=self.group_owner, parent=self.root)
        GroupMembership.objects.create(group=self.group, user=self.group_owner, role=ADMIN)
        GroupMembership.objects.create(group=self.group, user=self.group_admin, role=ADMIN)
        self.child = Group.objects.create(name="Child", owner=self.group_owner, parent=self.group)

        # A top-level group none of the users above administers.
        self.unrelated = Group.objects.create(name="Unrelated", owner=self.other)
        GroupMembership.objects.create(group=self.unrelated, user=self.other, role=ADMIN)
        # A top-level group group_admin also administers.
        self.target = Group.objects.create(name="Target", owner=self.other)
        GroupMembership.objects.create(group=self.target, user=self.other, role=ADMIN)

        self.client = APIClient()

    def _patch(self, user, group, parent):
        self.client.force_authenticate(user)
        return self.client.patch(f"/api/v1/groups/{group.pk}/", {"parent": parent}, format="json")

    def _parent_id(self, group):
        group.refresh_from_db()
        return group.parent_id

    # --- new parent ---------------------------------------------------------

    def test_admin_of_group_but_not_new_parent_is_denied(self):
        r = self._patch(self.group_admin, self.group, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_admin_of_group_and_new_parent_succeeds(self):
        GroupMembership.objects.create(group=self.target, user=self.group_admin, role=ADMIN)
        r = self._patch(self.group_admin, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(r.json()["parent"], self.target.pk)
        self.assertEqual(self._parent_id(self.group), self.target.pk)

    def test_admin_of_new_parents_ancestor_succeeds(self):
        GroupMembership.objects.create(group=self.target, user=self.group_admin, role=ADMIN)
        sub = Group.objects.create(name="TargetSub", owner=self.other, parent=self.target)
        r = self._patch(self.group_admin, self.group, sub.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(self._parent_id(self.group), sub.pk)

    def test_non_admin_member_of_new_parent_is_denied(self):
        GroupMembership.objects.create(group=self.target, user=self.group_admin, role="member")
        r = self._patch(self.group_admin, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_unknown_parent_id_gets_same_response_as_unadministered_parent(self):
        missing_pk = Group.objects.order_by("-pk").first().pk + 1000
        r_missing = self._patch(self.group_admin, self.group, missing_pk)
        r_unrelated = self._patch(self.group_admin, self.group, self.unrelated.pk)
        self.assertEqual(r_missing.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r_missing.status_code, r_unrelated.status_code)
        self.assertEqual(r_missing.json(), r_unrelated.json())
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_non_admin_of_group_is_denied_before_parent_checks(self):
        GroupMembership.objects.create(group=self.group, user=self.other, role="member")
        r = self._patch(self.other, self.group, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_put_with_new_parent_applies_same_rule(self):
        self.client.force_authenticate(self.group_admin)
        r = self.client.put(
            f"/api/v1/groups/{self.group.pk}/",
            {"name": "Team", "parent": self.unrelated.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    # --- self / subgroup ----------------------------------------------------

    def test_self_as_parent_is_rejected(self):
        r = self._patch(self.group_owner, self.group, self.group.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent", r.json())
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_subgroup_as_parent_is_rejected(self):
        r = self._patch(self.group_owner, self.group, self.child.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent", r.json())
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_deeper_subgroup_as_parent_is_rejected(self):
        grandchild = Group.objects.create(name="Grand", owner=self.group_owner, parent=self.child)
        r = self._patch(self.root_admin, self.root, grandchild.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(self._parent_id(self.root))

    # --- top-level ----------------------------------------------------------

    def test_owner_can_make_group_top_level(self):
        r = self._patch(self.group_owner, self.group, None)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIsNone(self._parent_id(self.group))

    def test_ancestor_admin_can_make_group_top_level(self):
        r = self._patch(self.root_admin, self.group, None)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIsNone(self._parent_id(self.group))

    def test_plain_group_admin_cannot_make_group_top_level(self):
        r = self._patch(self.group_admin, self.group, None)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_content_admin_can_make_group_top_level(self):
        staff = User.objects.create_user(username="staff", password="pw", can_access_all_content=True)
        r = self._patch(staff, self.group, None)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIsNone(self._parent_id(self.group))

    # --- unchanged parent ---------------------------------------------------

    def test_resending_current_parent_is_not_a_change(self):
        self.client.force_authenticate(self.group_admin)
        r = self.client.patch(
            f"/api/v1/groups/{self.group.pk}/",
            {"name": "Renamed", "parent": self.root.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_resending_null_parent_on_top_level_group_is_not_a_change(self):
        GroupMembership.objects.create(group=self.target, user=self.group_admin, role=ADMIN)
        self.client.force_authenticate(self.group_admin)
        r = self.client.put(
            f"/api/v1/groups/{self.target.pk}/",
            {"name": "Target 2", "parent": None},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_rename_without_parent_by_plain_admin_succeeds(self):
        self.client.force_authenticate(self.group_admin)
        r = self.client.patch(f"/api/v1/groups/{self.group.pk}/", {"name": "X"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)


class GroupParentDepthTests(TestCase):
    """The resulting tree stays within ``_GROUP_TRAVERSAL_MAX_DEPTH`` levels."""

    def setUp(self):
        self.user = User.objects.create_user(username="u", password="pw")
        self.chain = []
        parent = None
        # A full-depth chain; the user administers its root, so every level.
        for i in range(_GROUP_TRAVERSAL_MAX_DEPTH):
            parent = Group.objects.create(name=f"L{i + 1}", owner=self.user, parent=parent)
            self.chain.append(parent)
        GroupMembership.objects.create(group=self.chain[0], user=self.user, role=ADMIN)
        self.mover = Group.objects.create(name="Mover", owner=self.user)
        GroupMembership.objects.create(group=self.mover, user=self.user, role=ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _patch(self, parent):
        return self.client.patch(f"/api/v1/groups/{self.mover.pk}/", {"parent": parent.pk}, format="json")

    def test_leaf_at_deepest_allowed_level_succeeds(self):
        r = self._patch(self.chain[-2])
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_move_below_deepest_level_is_rejected(self):
        r = self._patch(self.chain[-1])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent", r.json())
        self.mover.refresh_from_db()
        self.assertIsNone(self.mover.parent_id)

    def test_subtree_that_would_exceed_depth_is_rejected(self):
        Group.objects.create(name="MoverChild", owner=self.user, parent=self.mover)
        r = self._patch(self.chain[-2])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.mover.refresh_from_db()
        self.assertIsNone(self.mover.parent_id)

    def test_subtree_that_fits_succeeds(self):
        Group.objects.create(name="MoverChild", owner=self.user, parent=self.mover)
        r = self._patch(self.chain[-3])
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_parent_chain_beyond_traversal_depth_is_rejected(self):
        # Built directly: a chain deeper than the cap (no API path enforces it on create).
        deeper = Group.objects.create(name="Deeper", owner=self.user, parent=self.chain[-1])
        # Direct admin, so the parent admin check passes on its own.
        GroupMembership.objects.create(group=deeper, user=self.user, role=ADMIN)
        r = self._patch(deeper)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
