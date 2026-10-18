"""Changing a group's ``parent`` through PATCH/PUT /api/v1/groups/{id}/ (#1657).

Pins the rules in ``GroupSerializer._check_parent_change``: who may change a
group's parent, admin on the new parent, no self or subgroup as parent, and the
``_GROUP_TRAVERSAL_MAX_DEPTH`` bound on the resulting tree.
"""

import threading
from unittest.mock import patch

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from groups.models import Group, GroupMembership, _GROUP_TRAVERSAL_MAX_DEPTH

ADMIN = GroupMembership.Role.ADMIN


CYCLE_MSG = "A group cannot be moved under itself or one of its subgroups."
DEPTH_MSG = f"Groups can be nested at most {_GROUP_TRAVERSAL_MAX_DEPTH} levels deep."


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
        # A second top-level group; tests grant admin on it where needed.
        self.target = Group.objects.create(name="Target", owner=self.other)
        GroupMembership.objects.create(group=self.target, user=self.other, role=ADMIN)

        self.client = APIClient()

    def _patch(self, user, group, parent):
        self.client.force_authenticate(user)
        return self.client.patch(f"/api/v1/groups/{group.pk}/", {"parent": parent}, format="json")

    def _parent_id(self, group):
        group.refresh_from_db()
        return group.parent_id

    def _admin_of(self, group, user):
        GroupMembership.objects.create(group=group, user=user, role=ADMIN)

    # --- authority over the group's place in the tree ---------------------

    def test_plain_group_admin_cannot_move_even_to_group_they_administer(self):
        self._admin_of(self.target, self.group_admin)
        r = self._patch(self.group_admin, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_new_top_level_group_cannot_be_used_to_move_a_subgroup(self):
        self.client.force_authenticate(self.group_admin)
        created = self.client.post("/api/v1/groups/", {"name": "Mine"}, format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        r = self._patch(self.group_admin, self.group, created.json()["id"])
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_owner_can_move_to_new_parent_they_administer(self):
        self._admin_of(self.target, self.group_owner)
        r = self._patch(self.group_owner, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(r.json()["parent"], self.target.pk)
        self.assertEqual(self._parent_id(self.group), self.target.pk)

    def test_ancestor_admin_can_move_to_new_parent_they_administer(self):
        self._admin_of(self.target, self.root_admin)
        r = self._patch(self.root_admin, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(self._parent_id(self.group), self.target.pk)

    def test_content_admin_can_move(self):
        staff = User.objects.create_user(username="staff", password="pw", can_access_all_content=True)
        r = self._patch(staff, self.group, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(self._parent_id(self.group), self.unrelated.pk)

    def test_top_level_group_move_by_owner_succeeds(self):
        self._admin_of(self.unrelated, self.root_admin)
        r = self._patch(self.root_admin, self.root, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(self._parent_id(self.root), self.unrelated.pk)

    def test_top_level_group_move_by_plain_admin_is_denied(self):
        self._admin_of(self.root, self.group_admin)
        self._admin_of(self.unrelated, self.group_admin)
        r = self._patch(self.group_admin, self.root, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIsNone(self._parent_id(self.root))

    def test_without_authority_unknown_and_existing_parent_answer_alike(self):
        missing_pk = Group.objects.order_by("-pk").first().pk + 1000
        r_missing = self._patch(self.group_admin, self.group, missing_pk)
        r_existing = self._patch(self.group_admin, self.group, self.unrelated.pk)
        self.assertEqual(r_missing.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r_existing.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r_missing.json(), r_existing.json())
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_non_admin_of_group_is_denied_before_parent_checks(self):
        GroupMembership.objects.create(group=self.group, user=self.other, role="member")
        r = self._patch(self.other, self.group, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    # --- admin on the new parent ------------------------------------------

    def test_owner_not_admin_of_new_parent_is_denied(self):
        r = self._patch(self.group_owner, self.group, self.unrelated.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent", r.json())
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_admin_of_new_parents_ancestor_counts(self):
        self._admin_of(self.target, self.group_owner)
        sub = Group.objects.create(name="TargetSub", owner=self.other, parent=self.target)
        r = self._patch(self.group_owner, self.group, sub.pk)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(self._parent_id(self.group), sub.pk)

    def test_non_admin_member_of_new_parent_is_denied(self):
        GroupMembership.objects.create(group=self.target, user=self.group_owner, role="member")
        r = self._patch(self.group_owner, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_unknown_parent_id_gets_same_response_as_unadministered_parent(self):
        missing_pk = Group.objects.order_by("-pk").first().pk + 1000
        r_missing = self._patch(self.group_owner, self.group, missing_pk)
        r_unrelated = self._patch(self.group_owner, self.group, self.unrelated.pk)
        self.assertEqual(r_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r_missing.status_code, r_unrelated.status_code)
        self.assertEqual(r_missing.json(), r_unrelated.json())
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_unknown_parent_id_on_top_level_group(self):
        # A top-level group's parent_id is None; an unknown id must still be refused.
        missing_pk = Group.objects.order_by("-pk").first().pk + 1000
        r = self._patch(self.root_admin, self.root, missing_pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(self._parent_id(self.root))

    def test_put_with_new_parent_applies_same_rule(self):
        self.client.force_authenticate(self.group_owner)
        r = self.client.put(
            f"/api/v1/groups/{self.group.pk}/",
            {"name": "Team", "parent": self.unrelated.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    # --- self / subgroup ----------------------------------------------------

    def test_self_as_parent_is_rejected(self):
        r = self._patch(self.group_owner, self.group, self.group.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [CYCLE_MSG]})
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_subgroup_as_parent_is_rejected(self):
        r = self._patch(self.group_owner, self.group, self.child.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [CYCLE_MSG]})
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_deeper_subgroup_as_parent_is_rejected(self):
        grandchild = Group.objects.create(name="Grand", owner=self.group_owner, parent=self.child)
        r = self._patch(self.root_admin, self.root, grandchild.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [CYCLE_MSG]})
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
        self._admin_of(self.target, self.group_admin)
        self.client.force_authenticate(self.group_admin)
        r = self.client.put(
            f"/api/v1/groups/{self.target.pk}/",
            {"name": "Target 2", "parent": None},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    # --- re-check under the row lock --------------------------------------

    def _move_after_validation(self, new_parent):
        from groups.serializers import GroupSerializer
        original = GroupSerializer.validate_parent

        def validate_then_move(serializer, value):
            result = original(serializer, value)
            Group.objects.filter(pk=self.group.pk).update(parent=new_parent)
            return result

        return patch.object(GroupSerializer, "validate_parent", validate_then_move)

    def test_echoed_parent_is_rechecked_under_lock(self):
        # The request echoes the parent it read; the group is moved by someone
        # else after validation and before the locked save. The echo is now a
        # move back under a group the caller does not administer.
        self._admin_of(self.target, self.group_admin)
        with self._move_after_validation(self.target):
            r = self._patch(self.group_admin, self.group, self.root.pk)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parent", r.json())
        # The simulated concurrent move ran inside the request's transaction,
        # so the refusal rolls it back too.
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_move_authority_is_rechecked_under_lock(self):
        # Validated as an ancestor admin; by the time the lock is taken the
        # group sits under a group the caller does not administer.
        self._admin_of(self.target, self.root_admin)
        with self._move_after_validation(self.unrelated):
            r = self._patch(self.root_admin, self.group, self.target.pk)
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._parent_id(self.group), self.root.pk)

    def test_rename_does_not_write_back_a_stale_parent(self):
        from groups.serializers import GroupSerializer
        self.assertFalse(hasattr(GroupSerializer, "validate_name"))

        def validate_then_move(serializer, value):
            Group.objects.filter(pk=self.group.pk).update(parent=self.target)
            return value

        with patch.object(GroupSerializer, "validate_name", validate_then_move, create=True):
            self.client.force_authenticate(self.group_admin)
            r = self.client.patch(f"/api/v1/groups/{self.group.pk}/", {"name": "Renamed"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Renamed")
        self.assertEqual(self.group.parent_id, self.target.pk)

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
        self.assertEqual(r.json(), {"parent": [DEPTH_MSG]})
        self.mover.refresh_from_db()
        self.assertIsNone(self.mover.parent_id)

    def test_subtree_that_would_exceed_depth_is_rejected(self):
        Group.objects.create(name="MoverChild", owner=self.user, parent=self.mover)
        r = self._patch(self.chain[-2])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [DEPTH_MSG]})
        self.mover.refresh_from_db()
        self.assertIsNone(self.mover.parent_id)

    def test_subtree_that_fits_succeeds(self):
        Group.objects.create(name="MoverChild", owner=self.user, parent=self.mover)
        r = self._patch(self.chain[-3])
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_parent_chain_beyond_traversal_depth_is_rejected(self):
        # Built directly: a chain deeper than the cap (a pre-#1690 tree; create now refuses it).
        deeper = Group.objects.create(name="Deeper", owner=self.user, parent=self.chain[-1])
        # Direct admin, so the parent admin check passes on its own.
        GroupMembership.objects.create(group=deeper, user=self.user, role=ADMIN)
        r = self._patch(deeper)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [DEPTH_MSG]})


class GroupCreateDepthTests(TestCase):
    """Creating a subgroup enforces the same bound as a parent change (#1690)."""

    def setUp(self):
        self.user = User.objects.create_user(username="u", password="pw")
        self.chain = []
        parent = None
        for i in range(_GROUP_TRAVERSAL_MAX_DEPTH):
            parent = Group.objects.create(name=f"L{i + 1}", owner=self.user, parent=parent)
            self.chain.append(parent)
        GroupMembership.objects.create(group=self.chain[0], user=self.user, role=ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _create(self, parent):
        return self.client.post(
            "/api/v1/groups/", {"name": "New", "parent": parent.pk}, format="json"
        )

    def test_subgroup_at_deepest_allowed_level_succeeds(self):
        # Parent is level N-1, so the new group is level N: allowed.
        r = self._create(self.chain[-2])
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)

    def test_subgroup_one_level_too_deep_is_rejected(self):
        before = Group.objects.count()
        r = self._create(self.chain[-1])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [DEPTH_MSG]})
        self.assertEqual(Group.objects.count(), before)

    def test_parent_beyond_traversal_depth_is_rejected(self):
        """A pre-#1690 tree deeper than the cap: refused via the level-count clause."""
        deeper = Group.objects.create(name="Deeper", owner=self.user, parent=self.chain[-1])
        # Direct admin: ancestor admin is not honored past the traversal depth.
        GroupMembership.objects.create(group=deeper, user=self.user, role=ADMIN)
        r = self._create(deeper)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": [DEPTH_MSG]})

    def test_non_admin_gets_permission_error_not_depth_error(self):
        outsider = User.objects.create_user(username="out", password="pw")
        self.client.force_authenticate(outsider)
        r = self._create(self.chain[-1])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json(), {"parent": ["Select a parent group you administer."]})

    def test_top_level_create_unaffected(self):
        r = self.client.post("/api/v1/groups/", {"name": "Top"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)


class GroupParentConcurrentMoveTests(TransactionTestCase):
    """Two crossing parent changes cannot both succeed.

    TransactionTestCase so the worker threads see committed rows; PostgreSQL
    only, since SQLite has no row locks.
    """

    def setUp(self):
        if connection.vendor == "sqlite":
            self.skipTest("Requires PostgreSQL row locks.")
        self.user = User.objects.create_user(username="mover", password="pw")
        self.x = Group.objects.create(name="X", owner=self.user)
        self.y = Group.objects.create(name="Y", owner=self.user)
        for g in (self.x, self.y):
            GroupMembership.objects.create(group=g, user=self.user, role=ADMIN)

    def test_crossing_moves_yield_exactly_one_success(self):
        results = []
        barrier = threading.Barrier(2)

        def attempt(group, parent):
            try:
                client = APIClient()
                client.force_authenticate(self.user)
                barrier.wait()
                r = client.patch(f"/api/v1/groups/{group.pk}/", {"parent": parent.pk}, format="json")
                results.append(r.status_code)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=attempt, args=(self.x, self.y)),
            threading.Thread(target=attempt, args=(self.y, self.x)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])
        self.x.refresh_from_db()
        self.y.refresh_from_db()
        self.assertEqual([self.x.parent_id is None, self.y.parent_id is None].count(True), 1)


class GroupCreateDepthConcurrentTests(TransactionTestCase):
    """A create racing a move cannot leave a tree over the depth cap (#1690).

    PostgreSQL only: SQLite has no row locks.
    """

    def setUp(self):
        if connection.vendor == "sqlite":
            self.skipTest("Requires PostgreSQL row locks.")
        self.user = User.objects.create_user(username="racer", password="pw")
        # Chain of N-1 levels: a leaf one short of the cap.
        self.chain = []
        parent = None
        for i in range(_GROUP_TRAVERSAL_MAX_DEPTH - 1):
            parent = Group.objects.create(name=f"C{i + 1}", owner=self.user, parent=parent)
            self.chain.append(parent)
        GroupMembership.objects.create(group=self.chain[0], user=self.user, role=ADMIN)
        # A childless top-level group: moving it under the leaf puts it at the
        # cap, so a subgroup created under it meanwhile would be one too deep.
        self.mover = Group.objects.create(name="Mover", owner=self.user)
        GroupMembership.objects.create(group=self.mover, user=self.user, role=ADMIN)

    def test_create_and_move_cannot_both_exceed_the_cap(self):
        # Creating under `mover` (new group at level 2) is fine on its own,
        # and so is moving the childless `mover` under the leaf (level N).
        # Together the new group would be level N+1: exactly one must lose.
        results = {}
        barrier = threading.Barrier(2)

        def create():
            try:
                client = APIClient()
                client.force_authenticate(self.user)
                barrier.wait()
                r = client.post(
                    "/api/v1/groups/", {"name": "New", "parent": self.mover.pk}, format="json"
                )
                results["create"] = r.status_code
            finally:
                connections.close_all()

        def move():
            try:
                client = APIClient()
                client.force_authenticate(self.user)
                barrier.wait()
                r = client.patch(
                    f"/api/v1/groups/{self.mover.pk}/",
                    {"parent": self.chain[-1].pk},
                    format="json",
                )
                results["move"] = r.status_code
            finally:
                connections.close_all()

        threads = [threading.Thread(target=create), threading.Thread(target=move)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        deepest = 0
        for g in Group.objects.all():
            depth, node = 1, g
            while node.parent_id is not None:
                depth, node = depth + 1, Group.objects.get(pk=node.parent_id)
            deepest = max(deepest, depth)
        self.assertLessEqual(deepest, _GROUP_TRAVERSAL_MAX_DEPTH, results)
        self.assertEqual(sorted(results.values()).count(status.HTTP_400_BAD_REQUEST), 1, results)
