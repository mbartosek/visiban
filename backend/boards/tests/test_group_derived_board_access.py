"""Group-derived board access is read from group membership.

Every surface that scopes boards through groups agrees with
``get_board_role()``: the board list and detail, board update and destroy,
board star, the nested card routes, the cross-board card query, and the
WebSocket access predicates.
"""
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.consumers import _lookup_role
from boards.models import Board, BoardFavorite, BoardMembership, Card, Column, Swimlane
from boards.permissions import get_board_role
from groups.consumers import _user_can_access_group_id
from groups.models import Group, GroupMembership, get_group_ids_for_board_access

BOARDS_URL = "/api/v1/boards/"
CARDS_URL = "/api/v1/cards/"


def _make_board_with_card(owner, group, name):
    board = Board.objects.create(name=name, owner=owner, group=group)
    BoardMembership.objects.create(board=board, user=owner, role=BoardMembership.Role.ADMIN)
    col = Column.objects.create(board=board, name="Backlog", position=0, allow_card_creation=True)
    swim = Swimlane.objects.create(board=board, name="General", position=0)
    card = Card.objects.create(
        board=board, column=col, swimlane=swim, created_by=owner,
        title=f"{name} card", position=0,
    )
    return board, card


def _results(response):
    return response.data["results"] if isinstance(response.data, dict) else response.data


class _GroupFixture(TestCase):
    def setUp(self):
        self.group_owner = User.objects.create_user(username="gowner", password="x")
        self.board_owner = User.objects.create_user(username="bowner", password="x")
        self.group = Group.objects.create(name="Team", owner=self.group_owner)
        self.subgroup = Group.objects.create(name="Sub", owner=self.group_owner, parent=self.group)
        GroupMembership.objects.create(
            group=self.group, user=self.board_owner, role=GroupMembership.Role.ADMIN,
        )
        self.board, self.card = _make_board_with_card(self.board_owner, self.group, "Team board")
        self.sub_board, self.sub_card = _make_board_with_card(
            self.board_owner, self.subgroup, "Sub board",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.group_owner)


class MembershipRequiredTests(_GroupFixture):
    """Without a membership row in the group or an ancestor, a user has no
    group-derived access to the group's boards."""

    def test_membership_required_for_board_role(self):
        self.assertIsNone(get_board_role(self.group_owner, self.board))
        self.assertIsNone(get_board_role(self.group_owner, self.sub_board))

    def test_membership_required_for_group_ids(self):
        self.assertEqual(get_group_ids_for_board_access(self.group_owner), set())

    def test_membership_required_for_card_query(self):
        r = self.client.get(CARDS_URL)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["results"], [])
        r = self.client.get(CARDS_URL, {"board": self.board.id})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["results"], [])

    def test_membership_required_for_board_list(self):
        r = self.client.get(BOARDS_URL)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {b["id"] for b in _results(r)}
        self.assertNotIn(self.board.id, ids)
        self.assertNotIn(self.sub_board.id, ids)

    def test_membership_required_for_board_retrieve(self):
        for board in (self.board, self.sub_board):
            r = self.client.get(f"{BOARDS_URL}{board.id}/")
            self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_membership_required_for_board_patch(self):
        r = self.client.patch(f"{BOARDS_URL}{self.board.id}/", {"name": "Renamed"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)
        self.board.refresh_from_db()
        self.assertEqual(self.board.name, "Team board")

    def test_membership_required_for_board_destroy(self):
        r = self.client.delete(f"{BOARDS_URL}{self.board.id}/")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Board.objects.filter(pk=self.board.pk).exists())

    def test_membership_required_for_star(self):
        r = self.client.post(f"{BOARDS_URL}{self.board.id}/star/")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(
            BoardFavorite.objects.filter(user=self.group_owner, board=self.board).exists()
        )
        r = self.client.delete(f"{BOARDS_URL}{self.board.id}/star/")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_membership_required_for_nested_card_routes(self):
        for board, card in ((self.board, self.card), (self.sub_board, self.sub_card)):
            r = self.client.get(f"{BOARDS_URL}{board.id}/cards/")
            self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)
            r = self.client.get(f"{BOARDS_URL}{board.id}/cards/{card.id}/")
            self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_membership_required_for_websocket_predicates(self):
        self.assertIsNone(_lookup_role(self.group_owner, self.board.id))
        self.assertIsNone(_lookup_role(self.group_owner, self.sub_board.id))
        self.assertFalse(_user_can_access_group_id(self.group_owner, self.group.id))

    def test_removing_membership_ends_access(self):
        membership = GroupMembership.objects.create(
            group=self.group, user=self.group_owner, role=GroupMembership.Role.ADMIN,
        )
        r = self.client.get(f"{BOARDS_URL}{self.board.id}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

        membership.delete()

        self.assertIsNone(get_board_role(self.group_owner, self.board))
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.board.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.client.get(CARDS_URL).data["results"], [])


class MemberAccessTests(_GroupFixture):
    """A group member has group-derived access to the group's boards and its
    subgroups' boards."""

    def setUp(self):
        super().setUp()
        GroupMembership.objects.create(
            group=self.group, user=self.group_owner, role=GroupMembership.Role.ADMIN,
        )

    def test_group_ids_include_group_and_subgroup(self):
        self.assertEqual(
            get_group_ids_for_board_access(self.group_owner),
            {self.group.id, self.subgroup.id},
        )

    def test_board_list_retrieve_and_cards(self):
        ids = {b["id"] for b in _results(self.client.get(BOARDS_URL))}
        self.assertTrue({self.board.id, self.sub_board.id} <= ids)
        for board in (self.board, self.sub_board):
            self.assertIsNotNone(get_board_role(self.group_owner, board))
            self.assertEqual(
                self.client.get(f"{BOARDS_URL}{board.id}/").status_code, status.HTTP_200_OK,
            )
            self.assertEqual(
                self.client.get(f"{BOARDS_URL}{board.id}/cards/").status_code, status.HTTP_200_OK,
            )
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.board.id}/cards/{self.card.id}/").status_code,
            status.HTTP_200_OK,
        )
        titles = {c["title"] for c in self.client.get(CARDS_URL).data["results"]}
        self.assertEqual(titles, {"Team board card", "Sub board card"})

    def test_star_succeeds(self):
        r = self.client.post(f"{BOARDS_URL}{self.board.id}/star/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(
            BoardFavorite.objects.filter(user=self.group_owner, board=self.board).exists()
        )

    def test_websocket_predicates_admit_member(self):
        self.assertIsNotNone(_lookup_role(self.group_owner, self.board.id))
        self.assertTrue(_user_can_access_group_id(self.group_owner, self.group.id))


class ViewerRoleTests(_GroupFixture):
    """A viewer-role group member can see the board but is below the role
    needed to change or delete it, so those actions answer 403."""

    def setUp(self):
        super().setUp()
        self.viewer = User.objects.create_user(username="viewer", password="x")
        GroupMembership.objects.create(
            group=self.group, user=self.viewer, role=GroupMembership.Role.VIEWER,
        )
        self.client.force_authenticate(self.viewer)

    def test_viewer_can_retrieve(self):
        r = self.client.get(f"{BOARDS_URL}{self.board.id}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_viewer_patch_is_403(self):
        r = self.client.patch(f"{BOARDS_URL}{self.board.id}/", {"name": "Renamed"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.board.refresh_from_db()
        self.assertEqual(self.board.name, "Team board")

    def test_viewer_destroy_is_403(self):
        r = self.client.delete(f"{BOARDS_URL}{self.board.id}/")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Board.objects.filter(pk=self.board.pk).exists())


class SubgroupMemberTests(_GroupFixture):
    """Membership in a subgroup grants access to that subgroup's boards and
    none on its parent group's boards."""

    def setUp(self):
        super().setUp()
        self.sub_member = User.objects.create_user(username="submember", password="x")
        GroupMembership.objects.create(
            group=self.subgroup, user=self.sub_member, role=GroupMembership.Role.MEMBER,
        )
        self.client.force_authenticate(self.sub_member)

    def test_subgroup_board_is_accessible(self):
        self.assertIsNotNone(get_board_role(self.sub_member, self.sub_board))
        r = self.client.get(f"{BOARDS_URL}{self.sub_board.id}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_parent_group_board_is_not_accessible(self):
        self.assertIsNone(get_board_role(self.sub_member, self.board))
        ids = {b["id"] for b in _results(self.client.get(BOARDS_URL))}
        self.assertNotIn(self.board.id, ids)
        self.assertIn(self.sub_board.id, ids)
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.board.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.board.id}/cards/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        titles = {c["title"] for c in self.client.get(CARDS_URL).data["results"]}
        self.assertEqual(titles, {"Sub board card"})
        self.assertIsNone(_lookup_role(self.sub_member, self.board.id))
        self.assertFalse(_user_can_access_group_id(self.sub_member, self.group.id))


class TraversalDepthTests(TestCase):
    """Group-derived access reaches exactly as far down a group tree as
    ``get_board_role()`` does, on every surface."""

    def setUp(self):
        from groups.models import _GROUP_TRAVERSAL_MAX_DEPTH

        self.member = User.objects.create_user(username="topmember", password="x")
        self.board_owner = User.objects.create_user(username="bowner", password="x")
        # One more level than the role ladder evaluates.
        self.levels = []
        parent = None
        for i in range(_GROUP_TRAVERSAL_MAX_DEPTH + 1):
            parent = Group.objects.create(name=f"L{i + 1}", owner=self.board_owner, parent=parent)
            self.levels.append(parent)
        GroupMembership.objects.create(
            group=self.levels[0], user=self.member, role=GroupMembership.Role.MEMBER,
        )
        self.reachable_board, _ = _make_board_with_card(
            self.board_owner, self.levels[-2], "Reachable",
        )
        self.deep_board, _ = _make_board_with_card(self.board_owner, self.levels[-1], "Deep")
        self.client = APIClient()
        self.client.force_authenticate(self.member)

    def test_role_ladder_reach(self):
        self.assertIsNotNone(get_board_role(self.member, self.reachable_board))
        self.assertIsNone(get_board_role(self.member, self.deep_board))

    def test_group_ids_match_role_ladder_reach(self):
        ids = get_group_ids_for_board_access(self.member)
        self.assertIn(self.levels[-2].id, ids)
        self.assertNotIn(self.levels[-1].id, ids)

    def test_board_list_and_detail_match_role_ladder_reach(self):
        ids = {b["id"] for b in _results(self.client.get(BOARDS_URL))}
        self.assertIn(self.reachable_board.id, ids)
        self.assertNotIn(self.deep_board.id, ids)
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.reachable_board.id}/").status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.deep_board.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{self.deep_board.id}/cards/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_card_query_matches_role_ladder_reach(self):
        titles = {c["title"] for c in self.client.get(CARDS_URL).data["results"]}
        self.assertEqual(titles, {"Reachable card"})

    def test_descendant_boards_match_role_ladder_reach(self):
        r = self.client.get(f"/api/v1/groups/{self.levels[0].id}/descendant-boards/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {b["id"] for b in r.data}
        self.assertIn(self.reachable_board.id, ids)
        self.assertNotIn(self.deep_board.id, ids)


class StoredRoleTests(_GroupFixture):
    """A stored role outside the model's choices grants nothing, on every
    surface, as it grants nothing in ``get_board_role()``."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="stored", password="x")
        self.client.force_authenticate(self.user)

    def _assert_no_access(self, board):
        self.assertIsNone(get_board_role(self.user, board))
        ids = {b["id"] for b in _results(self.client.get(BOARDS_URL))}
        self.assertNotIn(board.id, ids)
        self.assertEqual(
            self.client.get(f"{BOARDS_URL}{board.id}/").status_code, status.HTTP_404_NOT_FOUND,
        )
        self.assertNotIn(
            board.id, {c["board"] for c in self.client.get(CARDS_URL).data["results"]},
        )

    def test_unknown_group_role_grants_nothing(self):
        GroupMembership.objects.create(group=self.group, user=self.user, role="bogus")
        self._assert_no_access(self.board)
        self._assert_no_access(self.sub_board)

    def test_unknown_board_role_grants_nothing(self):
        BoardMembership.objects.create(board=self.board, user=self.user, role="bogus")
        self._assert_no_access(self.board)

    def test_unknown_board_role_is_not_overridden_by_group_membership(self):
        GroupMembership.objects.create(
            group=self.group, user=self.user, role=GroupMembership.Role.MEMBER,
        )
        BoardMembership.objects.create(board=self.board, user=self.user, role="bogus")
        self._assert_no_access(self.board)
        self.assertIsNotNone(get_board_role(self.user, self.sub_board))

    def test_board_owner_is_admitted_with_unknown_own_board_role(self):
        from boards.views._helpers import get_accessible_boards_queryset

        BoardMembership.objects.filter(board=self.board, user=self.board_owner).update(role="bogus")
        self.assertIsNotNone(get_board_role(self.board_owner, Board.objects.get(pk=self.board.pk)))
        self.assertIn(self.board, get_accessible_boards_queryset(self.board_owner))
        self.assertIn(self.sub_board, get_accessible_boards_queryset(self.board_owner))
