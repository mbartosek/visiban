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


class CanPlaceBoardsDepthTests(TestCase):
    """Every single-group response computes the flag at a cost independent of depth.

    The join path is covered by ``JoinPostQueryCountTests`` in
    ``test_group_invite_creator_1510.py``.
    """

    _n = 0

    def _chain(self, depth):
        CanPlaceBoardsDepthTests._n += 1
        n = self._n
        actor = User.objects.create_user(username=f"actor{n}", password="x")
        heir = User.objects.create_user(username=f"heir{n}", password="x")
        root = Group.objects.create(name=f"root{n}", owner=actor)
        GroupMembership.objects.create(group=root, user=actor, role=Role.ADMIN)
        node = root
        for i in range(depth):
            node = Group.objects.create(name=f"n{n}-{i}", owner=actor, parent=node)
        GroupMembership.objects.create(group=node, user=heir, role=Role.ADMIN)
        client = APIClient()
        client.force_authenticate(actor)
        return client, node, heir

    def _measure(self, request):
        counts = []
        request(*self._chain(1))  # warm one-time caches
        for depth in (1, 5):
            client, leaf, heir = self._chain(depth)
            with CaptureQueriesContext(connection) as ctx:
                response = request(client, leaf, heir)
            self.assertLess(response.status_code, 300, response.content)
            self.assertTrue(response.json()["can_place_boards"])
            counts.append(len(ctx.captured_queries))
        self.assertEqual(counts[0], counts[1])

    def test_retrieve(self):
        self._measure(lambda c, g, h: c.get(f"/api/v1/groups/{g.id}/"))

    def test_create_subgroup(self):
        self._measure(lambda c, g, h: c.post(
            "/api/v1/groups/", {"name": "New", "parent": g.id}, format="json",
        ))

    def test_update(self):
        self._measure(lambda c, g, h: c.patch(
            f"/api/v1/groups/{g.id}/", {"name": "Renamed"}, format="json",
        ))

    def test_board_defaults(self):
        self._measure(lambda c, g, h: c.patch(
            f"/api/v1/groups/{g.id}/board-defaults/", {"allowed_priorities": ["high"]},
            format="json",
        ))

    def test_transfer_ownership(self):
        self._measure(lambda c, g, h: c.post(
            f"/api/v1/groups/{g.id}/transfer-ownership/",
            {"new_owner_id": h.id, "confirmation": g.name}, format="json",
        ))


class CanPlaceBoardsBroadcastTests(TestCase):
    """Group events never carry the per-viewer flag; REST responses do (#1692)."""

    def setUp(self):
        self.actor = User.objects.create_user(username="actor", password="x")
        self.heir = User.objects.create_user(username="heir", password="x")
        self.group = Group.objects.create(name="Team", owner=self.actor)
        GroupMembership.objects.create(group=self.group, user=self.actor, role=Role.ADMIN)
        GroupMembership.objects.create(group=self.group, user=self.heir, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def test_no_group_event_payload_carries_flag(self):
        from unittest import mock

        layer = mock.Mock()
        layer.group_send = mock.AsyncMock()
        gid = self.group.id
        calls = [
            lambda: self.client.post("/api/v1/groups/", {"name": "Child", "parent": gid}, format="json"),
            lambda: self.client.patch(f"/api/v1/groups/{gid}/", {"name": "Team 2"}, format="json"),
            lambda: self.client.patch(
                f"/api/v1/groups/{gid}/board-defaults/", {"allowed_priorities": ["high"]}, format="json",
            ),
            lambda: self.client.post(
                f"/api/v1/groups/{gid}/transfer-ownership/",
                {"new_owner_id": self.heir.id, "confirmation": "Team 2"}, format="json",
            ),
        ]
        with mock.patch("groups.broadcast.get_channel_layer", return_value=layer):
            for call in calls:
                with self.captureOnCommitCallbacks(execute=True):
                    response = call()
                self.assertLess(response.status_code, 300, response.content)
                self.assertIn("can_place_boards", response.json())
            # The Django admin transfer path builds its payload with no viewer.
            from groups.admin import GroupAdmin
            from django.contrib.admin.sites import site

            with self.captureOnCommitCallbacks(execute=True):
                GroupAdmin(Group, site)._broadcast_after_commit(gid)

        frames = [c.args[1]["payload"] for c in layer.group_send.call_args_list]
        group_frames = [f for f in frames if f["event"].startswith("group.")]
        self.assertGreaterEqual(len(group_frames), 5)
        for frame in group_frames:
            self.assertNotIn("can_place_boards", frame["data"], frame["event"])
        # Other group-shaped fields still arrive.
        self.assertTrue(all("navigation_only" in f["data"] for f in group_frames))

    def test_strip_leaves_other_payloads_alone(self):
        from groups.broadcast import without_viewer_specific_group_fields

        self.assertEqual(without_viewer_specific_group_fields([1]), [1])
        payload = {"id": 1}
        self.assertIs(without_viewer_specific_group_fields(payload), payload)
        self.assertEqual(
            without_viewer_specific_group_fields({"id": 1, "can_place_boards": True}), {"id": 1},
        )
