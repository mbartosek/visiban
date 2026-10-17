"""``can_place_boards`` on group payloads matches the board-placement rule (#1692).

Placing a board in a group (``POST /groups/{id}/boards/``, move-group, import)
requires group admin on the group or one of its ancestors
(``user_is_group_admin``). The flag reports that same predicate, in bulk for
lists, so the move-board picker can disable groups the server would refuse.
"""
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board
from groups.models import Group, GroupMembership, group_admin_resolver, user_is_group_admin
from groups.serializers import GroupSerializer, full_group_access

Role = GroupMembership.Role


class CanPlaceBoardsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="gowner", password="x")
        self.user = User.objects.create_user(username="actor", password="x")
        self.parent = Group.objects.create(name="Parent", owner=self.owner)
        self.sub = Group.objects.create(name="Sub", owner=self.owner, parent=self.parent)
        GroupMembership.objects.create(group=self.parent, user=self.owner, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _join(self, group, role):
        GroupMembership.objects.update_or_create(
            group=group, user=self.user, defaults={"role": role},
        )

    def _list(self):
        response = self.client.get("/api/v1/groups/")
        self.assertEqual(response.status_code, 200, response.content)
        return {g["id"]: g["can_place_boards"] for g in response.json()["results"]}

    def _move(self, group_id):
        board = Board.objects.create(name="Mine", owner=self.user)
        return self.client.post(
            f"/api/v1/boards/{board.pk}/move-group/", {"group_id": group_id}, format="json",
        ).status_code

    # -- values by role --------------------------------------------------

    def test_direct_admin_true(self):
        self._join(self.sub, Role.ADMIN)
        rows = self._list()
        self.assertTrue(rows[self.sub.id])
        # Admin of the subgroup only: no right on the ancestor shown for navigation.
        self.assertFalse(rows[self.parent.id])

    def test_ancestor_admin_inherits_to_subgroup(self):
        self._join(self.parent, Role.ADMIN)
        self.assertEqual(self._list(), {self.parent.id: True, self.sub.id: True})

    def test_below_admin_roles_false(self):
        for role in (Role.VIEWER, Role.COLLABORATOR, Role.MEMBER):
            with self.subTest(role=role):
                self._join(self.parent, role)
                self.assertEqual(self._list(), {self.parent.id: False, self.sub.id: False})

    def test_non_member_sees_no_groups(self):
        self.assertEqual(self._list(), {})

    def test_all_content_user_true_everywhere(self):
        self.user.can_access_all_content = True
        self.user.save(update_fields=["can_access_all_content"])
        self.assertEqual(self._list(), {self.parent.id: True, self.sub.id: True})

    def test_retrieve_and_subgroups_carry_flag(self):
        self._join(self.parent, Role.MEMBER)
        self._join(self.sub, Role.ADMIN)
        detail = self.client.get(f"/api/v1/groups/{self.parent.id}/").json()
        self.assertFalse(detail["can_place_boards"])
        detail = self.client.get(f"/api/v1/groups/{self.sub.id}/").json()
        self.assertTrue(detail["can_place_boards"])
        subgroups = self.client.get(f"/api/v1/groups/{self.parent.id}/subgroups/").json()
        self.assertEqual([(g["id"], g["can_place_boards"]) for g in subgroups], [(self.sub.id, True)])

    # -- agreement with enforcement --------------------------------------

    def test_flag_agrees_with_move_group(self):
        for role in (Role.VIEWER, Role.COLLABORATOR, Role.MEMBER, Role.ADMIN):
            for target in (self.parent, self.sub):
                with self.subTest(role=role, target=target.name):
                    GroupMembership.objects.filter(user=self.user).delete()
                    self._join(target, role)
                    for group_id, can_place in self._list().items():
                        code = self._move(group_id)
                        self.assertEqual(code == 200, can_place, (group_id, code))
                        if not can_place:
                            self.assertEqual(code, 403)

    def test_resolver_matches_single_group_predicate(self):
        third = Group.objects.create(name="Third", owner=self.owner, parent=self.sub)
        self._join(self.sub, Role.ADMIN)
        is_admin = group_admin_resolver(self.user)
        for group in (self.parent, self.sub, third):
            group = Group.objects.select_related("parent__parent").get(pk=group.pk)
            self.assertEqual(is_admin(group), user_is_group_admin(self.user, group), group.name)

    # -- serializer without a viewer -------------------------------------

    def test_serializer_without_viewer_fails_closed(self):
        data = GroupSerializer(self.parent, context={"group_access": full_group_access}).data
        self.assertFalse(data["can_place_boards"])

    # -- query count -----------------------------------------------------

    def test_list_query_count_flat_as_rows_grow(self):
        # Same tree shape before and after (a direct membership on a parent and
        # on a child, plus a child reached only by descent), so the accessible-id walk issues the same queries; only
        # the row count changes.
        self._join(self.parent, Role.MEMBER)
        self._join(self.sub, Role.ADMIN)
        Group.objects.create(name="Member base", owner=self.owner, parent=self.parent)

        def count():
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get("/api/v1/groups/")
            self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        baseline = count()
        for i in range(3):
            admin_of = Group.objects.create(name=f"Admin {i}", owner=self.owner, parent=self.parent)
            self._join(admin_of, Role.ADMIN)
            Group.objects.create(name=f"Member {i}", owner=self.owner, parent=self.parent)
        rows = self._list()
        self.assertEqual(len(rows), 9)
        self.assertEqual(sum(rows.values()), 4)
        self.assertEqual(count(), baseline)
