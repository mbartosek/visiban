"""Tests verifying that viewer-role users cannot be assigned to cards.

The fix in CardSerializer.__init__ scopes the assignee_id queryset via
_get_assignable_member_ids(), which excludes viewer-role memberships.
These tests confirm the serializer rejects viewer IDs as invalid PKs and
accepts admin, member, and collaborator IDs.

Also covers the clear_viewer_assignees management command which back-fills
cards that were assigned to viewers before the serializer fix.
"""

import io
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership, Card, Column, Swimlane


PATCH_BROADCAST = "boards.broadcast.broadcast_board_event"


def _make_board(owner):
    board = Board.objects.create(name="Test Board", owner=owner)
    BoardMembership.objects.create(board=board, user=owner, role=BoardMembership.Role.ADMIN)
    col = Column.objects.create(board=board, name="Backlog", position=0, allow_card_creation=True)
    swim = Swimlane.objects.create(board=board, name="General", position=0)
    return board, col, swim


def _make_card(board, col, swim, created_by, title="Test Card"):
    return Card.objects.create(
        board=board,
        column=col,
        swimlane=swim,
        title=title,
        created_by=created_by,
        position=0,
    )


class ViewerAssigneeRejectionTests(TestCase):
    """Verify that the assignee_id field rejects viewer-role users."""

    def setUp(self):
        self.admin_user = User.objects.create_user(username="admin_user", password="pass")
        self.member_user = User.objects.create_user(username="member_user", password="pass")
        self.collaborator_user = User.objects.create_user(username="collab_user", password="pass")
        self.viewer_user = User.objects.create_user(username="viewer_user", password="pass")

        self.board, self.col, self.swim = _make_board(self.admin_user)
        # admin_user already has ADMIN membership via _make_board
        BoardMembership.objects.create(
            board=self.board, user=self.member_user, role=BoardMembership.Role.MEMBER
        )
        BoardMembership.objects.create(
            board=self.board, user=self.collaborator_user, role=BoardMembership.Role.COLLABORATOR
        )
        BoardMembership.objects.create(
            board=self.board, user=self.viewer_user, role=BoardMembership.Role.VIEWER
        )

        self.card = _make_card(self.board, self.col, self.swim, self.admin_user)

        self.client = APIClient()
        self.client.force_authenticate(self.admin_user)

    def _patch_card(self, payload):
        return self.client.patch(
            f"/api/v1/boards/{self.board.id}/cards/{self.card.id}/",
            payload,
            format="json",
        )

    @patch(PATCH_BROADCAST)
    def test_viewer_cannot_be_assigned_to_card(self, _):
        """PATCH with a viewer-role user ID must be rejected with HTTP 400."""
        r = self._patch_card({"assignee_id": self.viewer_user.id})
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(PATCH_BROADCAST)
    def test_member_can_be_assigned_to_card(self, _):
        """PATCH with a member-role user ID must succeed."""
        r = self._patch_card({"assignee_id": self.member_user.id})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["assignee"]["id"], self.member_user.id)

    @patch(PATCH_BROADCAST)
    def test_admin_can_be_assigned_to_card(self, _):
        """PATCH with an admin-role user ID must succeed."""
        r = self._patch_card({"assignee_id": self.admin_user.id})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["assignee"]["id"], self.admin_user.id)

    @patch(PATCH_BROADCAST)
    def test_collaborator_can_be_assigned_to_card(self, _):
        """PATCH with a collaborator-role user ID must succeed."""
        r = self._patch_card({"assignee_id": self.collaborator_user.id})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["assignee"]["id"], self.collaborator_user.id)

    @patch(PATCH_BROADCAST)
    def test_all_content_user_without_membership_can_be_assigned(self, _):
        """Intended assignable set (#1716): instance-wide content access keeps a
        user assignable on any board."""
        all_content = User.objects.create_user(
            username="all_content_user", password="pass", can_access_all_content=True
        )
        self.assertFalse(
            BoardMembership.objects.filter(board=self.board, user=all_content).exists()
        )
        r = self._patch_card({"assignee_id": all_content.id})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["assignee"]["id"], all_content.id)

    @patch(PATCH_BROADCAST)
    def test_plain_non_member_cannot_be_assigned(self, _):
        """A user with no membership and no instance-wide access is rejected."""
        outsider = User.objects.create_user(username="outsider_user", password="pass")
        r = self._patch_card({"assignee_id": outsider.id})
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)


class AssigneeFailClosedTests(TestCase):
    """assignee_id is declared with queryset=User.objects.none() and re-scoped in
    __init__ only when `board` is in context. A serializer built without board
    context must therefore reject every assignee rather than fall back to the old
    User.objects.all() (which exposed all users as candidates) (#1050)."""

    def setUp(self):
        self.user = User.objects.create_user(username="fc_admin", password="pass")
        self.board, self.col, self.swim = _make_board(self.user)

    def test_assignee_queryset_is_empty_without_board_context(self):
        from boards.serializers import CardSerializer
        s = CardSerializer()
        self.assertEqual(
            list(s.fields["assignee_id"].queryset),
            [],
            "Without board context the assignee queryset must fail closed (none()).",
        )

    def test_assignee_queryset_scoped_to_members_with_board_context(self):
        from boards.serializers import CardSerializer
        s = CardSerializer(context={"board": self.board})
        self.assertIn(
            self.user,
            list(s.fields["assignee_id"].queryset),
            "With board context the queryset must include the board's assignable members.",
        )

    @patch(PATCH_BROADCAST)
    def test_validation_rejects_assignee_when_board_missing(self, _):
        """is_valid() must reject a known user id when no board is in context."""
        from boards.serializers import CardSerializer
        s = CardSerializer(
            data={"title": "X", "assignee_id": self.user.id},
            context={},
        )
        s.is_valid()
        self.assertIn("assignee_id", s.errors)


class ClearViewerAssigneesCommandTests(TestCase):
    """Tests for the clear_viewer_assignees management command."""

    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.viewer = User.objects.create_user(username="viewer", password="pass")
        self.member = User.objects.create_user(username="member", password="pass")

        self.board = Board.objects.create(name="Board A", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        BoardMembership.objects.create(
            board=self.board, user=self.viewer, role=BoardMembership.Role.VIEWER
        )
        BoardMembership.objects.create(
            board=self.board, user=self.member, role=BoardMembership.Role.MEMBER
        )

        col = Column.objects.create(
            board=self.board, name="Todo", position=0, allow_card_creation=True
        )
        swim = Swimlane.objects.create(board=self.board, name="General", position=0)

        self.stale_card = Card.objects.create(
            board=self.board,
            column=col,
            swimlane=swim,
            title="Stale",
            created_by=self.owner,
            assignee=self.viewer,
            position=0,
        )
        self.clean_card = Card.objects.create(
            board=self.board,
            column=col,
            swimlane=swim,
            title="Clean",
            created_by=self.owner,
            assignee=self.member,
            position=1,
        )

    def _call(self, *args, **kwargs):
        out = io.StringIO()
        call_command("clear_viewer_assignees", *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_dry_run_reports_card_without_modifying(self):
        """--dry-run prints the affected card but does not clear the assignee."""
        output = self._call("--dry-run")
        self.assertIn("[DRY RUN]", output)
        self.assertIn(self.stale_card.title, output)
        # assignee must be unchanged after dry run
        self.stale_card.refresh_from_db()
        self.assertEqual(self.stale_card.assignee_id, self.viewer.id)

    def test_live_run_clears_viewer_assignee(self):
        """Running without --dry-run clears the assignee on affected cards."""
        output = self._call()
        self.assertIn("Cleared assignee on 1 card(s)", output)
        self.stale_card.refresh_from_db()
        self.assertIsNone(self.stale_card.assignee_id)

    def test_live_run_preserves_non_viewer_assignee(self):
        """Member-assigned cards must not be touched."""
        self._call()
        self.clean_card.refresh_from_db()
        self.assertEqual(self.clean_card.assignee_id, self.member.id)

    def test_board_flag_restricts_to_single_board(self):
        """--board <id> only scans the specified board."""
        other_board = Board.objects.create(name="Other", owner=self.owner)
        BoardMembership.objects.create(
            board=other_board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        BoardMembership.objects.create(
            board=other_board, user=self.viewer, role=BoardMembership.Role.VIEWER
        )
        col2 = Column.objects.create(
            board=other_board, name="Todo", position=0, allow_card_creation=True
        )
        swim2 = Swimlane.objects.create(board=other_board, name="General", position=0)
        other_stale = Card.objects.create(
            board=other_board,
            column=col2,
            swimlane=swim2,
            title="Other Stale",
            created_by=self.owner,
            assignee=self.viewer,
            position=0,
        )

        # Only scan board A — other_board's card should be untouched
        self._call("--board", str(self.board.pk))

        other_stale.refresh_from_db()
        self.assertEqual(other_stale.assignee_id, self.viewer.id)

        self.stale_card.refresh_from_db()
        self.assertIsNone(self.stale_card.assignee_id)

    def test_board_owner_without_non_viewer_membership_not_cleared(self):
        """Board owner is exempt even if they hold only a viewer membership.

        The command adds board.owner_id to non_viewer_ids unconditionally.
        This covers the edge case where the owner's direct membership is VIEWER.
        """
        # Create a fresh board where the owner has only a VIEWER membership
        # (bypasses the normal admin-membership-on-creation flow).
        owner2 = User.objects.create_user(username="owner2", password="pass")
        board2 = Board.objects.create(name="Board B", owner=owner2)
        BoardMembership.objects.create(
            board=board2, user=owner2, role=BoardMembership.Role.VIEWER
        )
        col2 = Column.objects.create(
            board=board2, name="Todo", position=0, allow_card_creation=True
        )
        swim2 = Swimlane.objects.create(board=board2, name="General", position=0)
        owner_card = Card.objects.create(
            board=board2,
            column=col2,
            swimlane=swim2,
            title="Owner card",
            created_by=owner2,
            assignee=owner2,
            position=0,
        )

        self._call()

        # Owner is in non_viewer_ids via owner_id exemption — card must be kept
        owner_card.refresh_from_db()
        self.assertEqual(owner_card.assignee_id, owner2.id)


class MemberSetLadderTests(TestCase):
    """The assignable and effective member sets follow the same role-resolution
    ladder as get_board_role: an explicit board row is judged alone, and group
    inheritance applies only to users with no explicit row."""

    def setUp(self):
        from groups.models import Group, GroupMembership

        self.GM = GroupMembership
        self.owner = User.objects.create_user(username="ladder_owner", password="pass")
        self.parent = Group.objects.create(name="Ladder parent", owner=self.owner)
        self.child = Group.objects.create(name="Ladder child", owner=self.owner, parent=self.parent)
        self.board, _col, _swim = _make_board(self.owner)
        self.board.group = self.child
        self.board.save()

    def _user(self, name):
        return User.objects.create_user(username=name, password="pass")

    def _ids(self):
        from boards.utils import _get_assignable_member_ids, _get_effective_member_ids

        board = Board.objects.get(pk=self.board.pk)
        return _get_assignable_member_ids(board), _get_effective_member_ids(board)

    def test_group_member_with_explicit_viewer_row_is_not_assignable(self):
        u = self._user("grp_member_viewer_row")
        self.GM.objects.create(group=self.child, user=u, role=self.GM.Role.MEMBER)
        BoardMembership.objects.create(board=self.board, user=u, role=BoardMembership.Role.VIEWER)
        assignable, effective = self._ids()
        self.assertNotIn(u.id, assignable)
        self.assertIn(u.id, effective)  # a viewer still has read access

    def test_group_member_without_explicit_row_stays_assignable(self):
        u = self._user("grp_member_no_row")
        self.GM.objects.create(group=self.child, user=u, role=self.GM.Role.MEMBER)
        assignable, effective = self._ids()
        self.assertIn(u.id, assignable)
        self.assertIn(u.id, effective)

    def test_group_viewer_is_not_assignable_but_is_effective(self):
        u = self._user("grp_viewer")
        self.GM.objects.create(group=self.child, user=u, role=self.GM.Role.VIEWER)
        assignable, effective = self._ids()
        self.assertNotIn(u.id, assignable)
        self.assertIn(u.id, effective)

    def test_ancestor_group_member_is_assignable(self):
        u = self._user("ancestor_member")
        self.GM.objects.create(group=self.parent, user=u, role=self.GM.Role.MEMBER)
        assignable, effective = self._ids()
        self.assertIn(u.id, assignable)
        self.assertIn(u.id, effective)

    def test_nearest_ancestor_role_decides(self):
        """Viewer in the child group, member in the parent: the nearest group
        wins, as in get_board_role, so the user is not assignable."""
        u = self._user("nearest_viewer")
        self.GM.objects.create(group=self.child, user=u, role=self.GM.Role.VIEWER)
        self.GM.objects.create(group=self.parent, user=u, role=self.GM.Role.MEMBER)
        assignable, _ = self._ids()
        self.assertNotIn(u.id, assignable)

    def test_explicit_member_row_overrides_group_viewer(self):
        u = self._user("explicit_member")
        self.GM.objects.create(group=self.child, user=u, role=self.GM.Role.VIEWER)
        BoardMembership.objects.create(board=self.board, user=u, role=BoardMembership.Role.MEMBER)
        assignable, _ = self._ids()
        self.assertIn(u.id, assignable)

    def test_out_of_enum_row_is_excluded_from_both_sets(self):
        """A stored role outside the enum grants no access, so the user is
        neither assignable nor a notification recipient, group or not."""
        direct = self._user("bad_role_direct")
        BoardMembership.objects.create(board=self.board, user=direct, role="bogus")
        grouped = self._user("bad_role_grouped")
        self.GM.objects.create(group=self.child, user=grouped, role=self.GM.Role.ADMIN)
        BoardMembership.objects.create(board=self.board, user=grouped, role="bogus")
        assignable, effective = self._ids()
        for u in (direct, grouped):
            self.assertNotIn(u.id, assignable)
            self.assertNotIn(u.id, effective)

    def test_sets_agree_with_get_board_role(self):
        from boards.permissions import get_board_role

        users = {}
        for name, grole, brole in [
            ("a", "member", "viewer"),
            ("b", "member", None),
            ("c", "viewer", None),
            ("d", "admin", "bogus"),
            ("e", "viewer", "collaborator"),
        ]:
            u = self._user(f"agree_{name}")
            self.GM.objects.create(group=self.child, user=u, role=grole)
            if brole:
                BoardMembership.objects.create(board=self.board, user=u, role=brole)
            users[u.id] = u
        assignable, effective = self._ids()
        board = Board.objects.select_related("group__parent").get(pk=self.board.pk)
        for uid, u in users.items():
            role = get_board_role(u, board)
            self.assertEqual(uid in effective, role is not None, u.username)
            self.assertEqual(
                uid in assignable,
                role in (BoardMembership.Role.ADMIN, BoardMembership.Role.MEMBER, BoardMembership.Role.COLLABORATOR),
                u.username,
            )

    def test_multi_ancestor_and_invalid_group_role_agree_with_get_board_role(self):
        from boards.permissions import get_board_role

        users = {}
        for name, child_role, parent_role in [
            ("m1", "viewer", "member"),    # nearest viewer wins
            ("m2", "member", "viewer"),    # nearest member wins
            ("m3", "bogus", "member"),     # invalid nearest role skipped
            ("m4", "bogus", None),         # invalid only: no access
            ("m5", "bogus", "viewer"),     # invalid skipped, viewer decides
        ]:
            u = self._user(f"anc_{name}")
            self.GM.objects.create(group=self.child, user=u, role=child_role)
            if parent_role:
                self.GM.objects.create(group=self.parent, user=u, role=parent_role)
            users[u.id] = u
        assignable, effective = self._ids()
        board = Board.objects.select_related("group__parent").get(pk=self.board.pk)
        for uid, u in users.items():
            role = get_board_role(u, board)
            self.assertEqual(uid in effective, role is not None, u.username)
            self.assertEqual(
                uid in assignable,
                role in (BoardMembership.Role.ADMIN, BoardMembership.Role.MEMBER, BoardMembership.Role.COLLABORATOR),
                u.username,
            )

    def test_query_count_is_bounded_in_user_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from boards.utils import _get_assignable_member_ids

        for i in range(8):
            u = self._user(f"bulk_{i}")
            self.GM.objects.create(group=self.child, user=u, role=self.GM.Role.MEMBER)
            BoardMembership.objects.create(board=self.board, user=u, role=BoardMembership.Role.VIEWER)
        board = Board.objects.select_related("group__parent").get(pk=self.board.pk)
        with CaptureQueriesContext(connection) as ctx:
            _get_assignable_member_ids(board)
        self.assertLessEqual(len(ctx), 3)


class ClearAssigneesLadderTests(TestCase):
    def test_clears_assignee_whose_explicit_viewer_row_overrides_group_role(self):
        from groups.models import Group, GroupMembership

        owner = User.objects.create_user(username="clr_owner", password="pass")
        u = User.objects.create_user(username="clr_user", password="pass")
        group = Group.objects.create(name="Clr group", owner=owner)
        GroupMembership.objects.create(group=group, user=u, role=GroupMembership.Role.MEMBER)
        board, col, swim = _make_board(owner)
        board.group = group
        board.save()
        BoardMembership.objects.create(board=board, user=u, role=BoardMembership.Role.VIEWER)
        card = _make_card(board, col, swim, owner)
        card.assignee = u
        card.save()
        call_command("clear_viewer_assignees", stdout=io.StringIO())
        card.refresh_from_db()
        self.assertIsNone(card.assignee_id)


class MemberSetImplicitAccessTests(TestCase):
    """Owner and instance-wide content access outrank any explicit row."""

    def test_owner_and_all_content_user_stay_in_both_sets_despite_odd_rows(self):
        from boards.utils import _get_assignable_member_ids, _get_effective_member_ids

        owner = User.objects.create_user(username="imp_owner", password="pass")
        admin = User.objects.create_user(
            username="imp_all", password="pass", can_access_all_content=True
        )
        board, _c, _s = _make_board(owner)
        BoardMembership.objects.filter(board=board, user=owner).update(role="viewer")
        BoardMembership.objects.create(board=board, user=admin, role="bogus")
        board = Board.objects.get(pk=board.pk)
        for fn in (_get_assignable_member_ids, _get_effective_member_ids):
            ids = fn(board)
            self.assertIn(owner.id, ids)
            self.assertIn(admin.id, ids)
            self.assertIn(admin.id, fn(board, site_admin_ids={admin.id}))



class ClearAssigneesScopeTests(TestCase):
    def test_keeps_assignee_who_is_no_longer_a_member(self):
        owner = User.objects.create_user(username="fm_owner", password="pass")
        former = User.objects.create_user(username="fm_former", password="pass")
        board, col, swim = _make_board(owner)
        card = _make_card(board, col, swim, owner)
        card.assignee = former  # no membership at all
        card.save()
        out = io.StringIO()
        call_command("clear_viewer_assignees", stdout=out)
        card.refresh_from_db()
        self.assertEqual(card.assignee_id, former.id)

    def test_still_clears_viewer_assignee(self):
        owner = User.objects.create_user(username="cv_owner", password="pass")
        viewer = User.objects.create_user(username="cv_viewer", password="pass")
        board, col, swim = _make_board(owner)
        BoardMembership.objects.create(board=board, user=viewer, role=BoardMembership.Role.VIEWER)
        card = _make_card(board, col, swim, owner)
        card.assignee = viewer
        card.save()
        call_command("clear_viewer_assignees", stdout=io.StringIO())
        card.refresh_from_db()
        self.assertIsNone(card.assignee_id)


class FullRosterLadderTests(TestCase):
    """/full/ roster roles match get_board_role."""

    def test_full_roster_roles_match_get_board_role(self):
        from groups.models import Group, GroupMembership
        from boards.permissions import get_board_role

        owner = User.objects.create_user(username="ros_owner", password="pass")
        parent = Group.objects.create(name="Ros parent", owner=owner)
        child = Group.objects.create(name="Ros child", owner=owner, parent=parent)
        board, _c, _s = _make_board(owner)
        board.group = child
        board.save()
        u1, u2, u3, u4 = (User.objects.create_user(username=f"ros_u{i}", password="pass") for i in range(1, 5))
        GroupMembership.objects.create(group=child, user=u1, role="viewer")
        GroupMembership.objects.create(group=parent, user=u1, role="member")
        GroupMembership.objects.create(group=child, user=u3, role="member")
        GroupMembership.objects.create(group=parent, user=u3, role="viewer")
        BoardMembership.objects.create(board=board, user=u2, role="bogus")
        GroupMembership.objects.create(group=child, user=u2, role="admin")
        # invalid nearest group role is skipped; the farther valid one decides
        GroupMembership.objects.create(group=child, user=u4, role="bogus")
        GroupMembership.objects.create(group=parent, user=u4, role="member")
        client = APIClient()
        client.force_authenticate(owner)
        with patch(PATCH_BROADCAST):
            resp = client.get(f"/api/v1/boards/{board.id}/full/")
        self.assertEqual(resp.status_code, 200)
        roster = {m["user"]["id"]: m["role"] for m in resp.json()["members"]}
        fresh = Board.objects.select_related("group__parent").get(pk=board.pk)
        for u in (u1, u3, u4):
            self.assertEqual(roster.get(u.id), get_board_role(u, fresh), u.username)
        self.assertEqual(roster[u1.id], "viewer")
        self.assertEqual(roster[u3.id], "member")
        self.assertEqual(roster[u4.id], "member")
        # out-of-enum explicit row stays listed so admins can remove it
        self.assertEqual(roster[u2.id], "bogus")
