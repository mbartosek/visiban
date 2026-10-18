"""Tests for the ``is_site_admin`` flag on board member rows (#1290).

The board member endpoints refuse to modify or remove a member whose
``User.is_site_admin`` is set unless the caller is a site admin too. The
member-management UI needs that same signal to decide which rows to lock, so
member rows carry ``is_site_admin`` as a sibling of ``role``. The resolved
``role`` is not a substitute: it reads ``site_admin`` for
``can_access_all_content``, which is an independent flag.

Visibility: board admins and site admins only (the only viewers with member
controls), on every surface that emits a member row — ``/full/``, the members
POST response, ``member.*`` WebSocket frames, and the change feed. The nested
``user`` object never carries it (``BoardUserSerializer`` stays public-only).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import User
from boards.consumers import BoardConsumer
from boards.models import Board, BoardMembership


def _user(username, **extra):
    return User.objects.create_user(username=username, password="pass", **extra)


class MemberSiteAdminFlagTestBase(TestCase):
    def setUp(self):
        self.owner = _user("sa_owner")
        self.board = Board.objects.create(name="Flag Board", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        self.member = _user("sa_member")
        BoardMembership.objects.create(
            board=self.board, user=self.member, role=BoardMembership.Role.MEMBER
        )
        # Mismatch case 1: a real site admin whose board role is an ordinary
        # explicit membership, so the row's `role` is "member".
        self.site_admin_member = _user("sa_real_admin", is_site_admin=True)
        BoardMembership.objects.create(
            board=self.board, user=self.site_admin_member, role=BoardMembership.Role.MEMBER
        )
        # Mismatch case 2: all-content access without site-admin status, so the
        # synthesized row's `role` is "site_admin" while the user is not one.
        self.all_content_user = _user("sa_all_content", can_access_all_content=True)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def full_rows(self, viewer):
        response = self.client_for(viewer).get(f"/api/v1/boards/{self.board.pk}/full/")
        self.assertEqual(response.status_code, 200)
        return {row["user"]["id"]: row for row in response.data["members"]}


class FullMembersFlagTests(MemberSiteAdminFlagTestBase):
    def test_admin_sees_real_site_admin_flag_not_resolved_role(self):
        rows = self.full_rows(self.owner)
        # Explicit membership with role "member" — flag says site admin.
        self.assertEqual(rows[self.site_admin_member.pk]["role"], "member")
        self.assertIs(rows[self.site_admin_member.pk]["is_site_admin"], True)
        self.assertIs(rows[self.member.pk]["is_site_admin"], False)
        self.assertIs(rows[self.owner.pk]["is_site_admin"], False)
        # An all-content user with no role on the board is listed for
        # site-admin readers only (#1714) — see the roster tests below.
        self.assertNotIn(self.all_content_user.pk, rows)
        # Resolved role "site_admin" from all-content access — flag says not.
        rows = self.full_rows(self.all_content_user)
        self.assertEqual(rows[self.all_content_user.pk]["role"], "site_admin")
        self.assertIs(rows[self.all_content_user.pk]["is_site_admin"], False)

    def test_site_admin_viewer_sees_flag(self):
        rows = self.full_rows(self.all_content_user)
        self.assertIs(rows[self.site_admin_member.pk]["is_site_admin"], True)

    def test_member_viewer_never_sees_flag(self):
        rows = self.full_rows(self.member)
        for row in rows.values():
            self.assertNotIn("is_site_admin", row)

    def test_flag_never_appears_on_nested_user(self):
        for row in self.full_rows(self.owner).values():
            self.assertNotIn("is_site_admin", row["user"])

    def test_flag_adds_no_query_per_all_content_user(self):
        """``is_site_admin`` is in the all-content user query's ``.only()``
        list, so reading it must not issue a deferred-field load per row."""
        url = f"/api/v1/boards/{self.board.pk}/full/"
        client = self.client_for(self.owner)
        client.get(url)  # warm any one-time caches
        with CaptureQueriesContext(connection) as before:
            client.get(url)
        for n in range(3):
            _user(f"sa_extra_all_content_{n}", can_access_all_content=True, is_site_admin=bool(n % 2))
        with CaptureQueriesContext(connection) as after:
            client.get(url)
        self.assertEqual(len(after), len(before))


class FlagMatchesServerEnforcementTests(MemberSiteAdminFlagTestBase):
    """The flag is exactly the signal the members endpoints enforce on."""

    def test_flag_true_row_is_refused_for_non_site_admin_caller(self):
        rows = self.full_rows(self.owner)
        self.assertIs(rows[self.site_admin_member.pk]["is_site_admin"], True)
        url = f"/api/v1/boards/{self.board.pk}/members/"
        response = self.client_for(self.owner).post(
            url, {"user_id": self.site_admin_member.pk, "role": "viewer"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        response = self.client_for(self.owner).delete(f"{url}{self.site_admin_member.pk}/")
        self.assertEqual(response.status_code, 403)

    def test_flag_false_row_with_site_admin_role_is_editable(self):
        rows = self.full_rows(self.all_content_user)
        self.assertIs(rows[self.all_content_user.pk]["is_site_admin"], False)
        response = self.client_for(self.owner).post(
            f"/api/v1/boards/{self.board.pk}/members/",
            {"user_id": self.all_content_user.pk, "role": "viewer"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        # A caller who is not a site admin never gets the flag (#1706).
        self.assertNotIn("is_site_admin", response.data)

    def test_site_admin_caller_may_edit_flagged_row_and_response_carries_flag(self):
        caller = _user("sa_caller", is_site_admin=True, can_access_all_content=True)
        response = self.client_for(caller).post(
            f"/api/v1/boards/{self.board.pk}/members/",
            {"user_id": self.site_admin_member.pk, "role": "viewer"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["is_site_admin"], True)
        self.assertNotIn("is_site_admin", response.data["user"])


class FullMembersRosterByReaderTests(MemberSiteAdminFlagTestBase):
    """Which rows ``/full/`` lists, and where ``is_site_admin`` appears, per reader (#1714).

    The member list holds the board's own members (direct, group-inherited,
    owner). All-content users with no role on the board are added for a
    site-admin reader only (board role ``site_admin`` or ``User.is_site_admin``).
    A board admin who is not a site admin gets the flag on rows backed by a
    membership only — the rows the members endpoints refuse to change for them.
    """

    def setUp(self):
        super().setUp()
        from groups.models import Group, GroupMembership

        # A board owned by a site admin with no membership row, inside a group
        # whose member is a site admin: two rows with no membership on the
        # board, each of whose flag would be True.
        self.site_admin_owner = _user("ro_site_admin_owner", is_site_admin=True)
        group = Group.objects.create(name="Roster Group", owner=self.site_admin_owner)
        self.site_admin_group_member = _user("ro_site_admin_group", is_site_admin=True)
        GroupMembership.objects.get_or_create(
            group=group, user=self.site_admin_group_member,
            defaults={"role": GroupMembership.Role.MEMBER},
        )
        self.board.owner = self.site_admin_owner
        self.board.group = group
        self.board.save(update_fields=["owner", "group"])
        # The previous owner keeps their explicit admin row: a board admin who
        # is not a site admin.
        self.board_admin = self.owner
        self.viewer = _user("ro_viewer")
        BoardMembership.objects.create(
            board=self.board, user=self.viewer, role=BoardMembership.Role.VIEWER
        )
        self.collaborator = _user("ro_collaborator")
        BoardMembership.objects.create(
            board=self.board, user=self.collaborator, role=BoardMembership.Role.COLLABORATOR
        )
        # A full site admin (both flags), not a member of the board.
        self.full_site_admin = _user(
            "ro_full_site_admin", is_site_admin=True, can_access_all_content=True
        )
        # An all-content user who also has a direct membership: listed for
        # everyone, with the membership role.
        self.all_content_member = _user("ro_all_content_member", can_access_all_content=True)
        BoardMembership.objects.create(
            board=self.board, user=self.all_content_member, role=BoardMembership.Role.MEMBER
        )
        # Board members, as every reader sees them.
        self.board_member_ids = {
            self.board_admin.pk, self.member.pk, self.site_admin_member.pk,
            self.viewer.pk, self.collaborator.pk, self.all_content_member.pk,
            self.site_admin_owner.pk, self.site_admin_group_member.pk,
        }
        # All-content users with no role on the board.
        self.instance_only_ids = {self.all_content_user.pk, self.full_site_admin.pk}

    def assert_board_members_only(self, rows):
        self.assertEqual(set(rows), self.board_member_ids)
        self.assertNotIn("site_admin", {row["role"] for row in rows.values()})
        self.assertEqual(rows[self.all_content_member.pk]["role"], "member")

    def assert_full_roster_with_flags(self, rows):
        self.assertEqual(set(rows), self.board_member_ids | self.instance_only_ids)
        for pk in self.instance_only_ids:
            self.assertEqual(rows[pk]["role"], "site_admin")
            self.assertIsNone(rows[pk]["id"])
        for row in rows.values():
            self.assertIn("is_site_admin", row)
        self.assertIs(rows[self.site_admin_owner.pk]["is_site_admin"], True)
        self.assertIs(rows[self.site_admin_group_member.pk]["is_site_admin"], True)
        self.assertIs(rows[self.full_site_admin.pk]["is_site_admin"], True)
        self.assertIs(rows[self.all_content_user.pk]["is_site_admin"], False)

    def test_member_reader_sees_board_members_without_flag(self):
        rows = self.full_rows(self.member)
        self.assert_board_members_only(rows)
        for row in rows.values():
            self.assertNotIn("is_site_admin", row)

    def test_viewer_and_collaborator_readers_see_board_members_without_flag(self):
        for reader in (self.viewer, self.collaborator):
            with self.subTest(reader=reader.username):
                rows = self.full_rows(reader)
                self.assert_board_members_only(rows)
                for row in rows.values():
                    self.assertNotIn("is_site_admin", row)

    def test_board_admin_reader_gets_flag_on_membership_rows_only(self):
        rows = self.full_rows(self.board_admin)
        self.assert_board_members_only(rows)
        for row in rows.values():
            if row["id"] is None:
                self.assertNotIn("is_site_admin", row)
            else:
                self.assertIn("is_site_admin", row)
        # The rows the endpoints refuse to change still carry the lock signal.
        self.assertIs(rows[self.site_admin_member.pk]["is_site_admin"], True)
        self.assertIs(rows[self.all_content_member.pk]["is_site_admin"], False)
        # Owner and group rows have no membership row: no flag.
        self.assertIsNone(rows[self.site_admin_owner.pk]["id"])
        self.assertIsNone(rows[self.site_admin_group_member.pk]["id"])

    def test_board_admin_flagless_rows_are_ordinary_targets(self):
        """A group row without the flag is one the endpoint does not refuse."""
        response = self.client_for(self.board_admin).post(
            f"/api/v1/boards/{self.board.pk}/members/",
            {"user_id": self.site_admin_group_member.pk, "role": "admin"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_site_admin_reader_not_a_member_gets_full_roster(self):
        rows = self.full_rows(self.full_site_admin)
        self.assert_full_roster_with_flags(rows)

    def test_all_content_reader_gets_full_roster(self):
        rows = self.full_rows(self.all_content_user)
        self.assert_full_roster_with_flags(rows)

    def test_all_content_reader_who_is_a_member_gets_full_roster(self):
        rows = self.full_rows(self.all_content_member)
        self.assert_full_roster_with_flags(rows)
        self.assertEqual(rows[self.all_content_member.pk]["role"], "member")

    def test_site_admin_board_admin_without_all_content_gets_full_roster(self):
        reader = _user("ro_site_admin_board_admin", is_site_admin=True)
        BoardMembership.objects.create(
            board=self.board, user=reader, role=BoardMembership.Role.ADMIN
        )
        self.board_member_ids.add(reader.pk)
        rows = self.full_rows(reader)
        self.assert_full_roster_with_flags(rows)

    def test_site_admin_member_reader_gets_roster_rows_but_no_flag(self):
        # The site admin with a member row: the roster follows the reader's
        # site role, the flag follows the board role (member: never shown).
        rows = self.full_rows(self.site_admin_member)
        self.assertEqual(set(rows), self.board_member_ids | self.instance_only_ids)
        for row in rows.values():
            self.assertNotIn("is_site_admin", row)

    def test_query_count_does_not_grow_with_all_content_users(self):
        """For a reader who gets the roster rows and one who does not, the
        ``/full/`` query count stays the same when another all-content user is
        added: both reuse the site-admin list get_cards() already loads."""
        url = f"/api/v1/boards/{self.board.pk}/full/"
        readers = (self.board_admin, self.full_site_admin)

        def counts():
            result = {}
            for reader in readers:
                client = self.client_for(reader)
                client.get(url)  # warm any one-time caches
                with CaptureQueriesContext(connection) as ctx:
                    client.get(url)
                result[reader.username] = len(ctx)
            return result

        before = counts()
        _user("ro_extra_all_content", can_access_all_content=True)
        self.assertEqual(counts(), before)


class SiteAdminRosterVisibleUnitTests(SimpleTestCase):
    def test_rule(self):
        from boards.permissions import SITE_ADMIN, site_admin_roster_visible

        self.assertTrue(site_admin_roster_visible(SITE_ADMIN, False))
        self.assertTrue(site_admin_roster_visible("admin", True))
        self.assertTrue(site_admin_roster_visible("member", True))
        for role in ("admin", "member", "collaborator", "viewer", None):
            self.assertFalse(site_admin_roster_visible(role, False))
        # Anything but a real True fails closed.
        self.assertFalse(site_admin_roster_visible("admin", None))
        self.assertFalse(site_admin_roster_visible("admin", 1))


class ChangeFeedFlagTests(MemberSiteAdminFlagTestBase):
    def setUp(self):
        super().setUp()
        self.target = _user("sa_feed_target")
        # Posted by a site admin: events from other callers omit the flag (#1706).
        poster = _user("sa_feed_poster", is_site_admin=True, can_access_all_content=True)
        response = self.client_for(poster).post(
            f"/api/v1/boards/{self.board.pk}/members/",
            {"user_id": self.target.pk, "role": "member"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.url = f"/api/v1/boards/{self.board.pk}/events/"

    def _member_added_row(self, viewer):
        rows = self.client_for(viewer).get(self.url).data["results"]
        return next(r for r in rows if r["event"] == "member.added")

    def test_admin_reader_sees_flag(self):
        self.assertIs(self._member_added_row(self.owner)["data"]["is_site_admin"], False)

    def test_member_reader_does_not_see_flag(self):
        row = self._member_added_row(self.member)
        self.assertNotIn("is_site_admin", row["data"])
        self.assertIn("role", row["data"])

    def test_target_reader_does_not_see_flag_on_own_row(self):
        """No self-row exception — a user's own flag comes from /auth/me/."""
        self.assertNotIn("is_site_admin", self._member_added_row(self.target)["data"])

    def test_unknown_role_fails_closed(self):
        from boards.models import BoardEvent
        from boards.serializers import BoardEventSerializer

        event = BoardEvent.objects.filter(board_id=self.board.pk, event="member.added").last()
        data = BoardEventSerializer(event, context={}).data
        self.assertNotIn("is_site_admin", data["data"])


class ConsumerFlagTests(SimpleTestCase):
    def _send(self, role, subscriber_id=5, event="member.updated"):
        consumer = BoardConsumer()
        consumer.board_id = 1
        consumer.room = "board_1"
        consumer.channel_name = "test-channel"
        consumer.channel_layer = AsyncMock()
        consumer.send = AsyncMock()
        consumer.close = AsyncMock()
        consumer.scope = {
            "url_route": {"kwargs": {"board_id": 1}},
            "user": MagicMock(is_authenticated=True, id=subscriber_id, must_change_password=False, must_change_username=False),
        }
        consumer._role = role
        # Self-subject frames re-resolve the role from the DB (#1332); this is
        # a SimpleTestCase, so pin the refresh to the role under test.
        consumer._refresh_role = AsyncMock(return_value=role)
        payload = {
            "event": event,
            "data": {
                "id": 9, "user": {"id": 7}, "role": "member",
                "is_moderator": True, "is_site_admin": True,
            },
        }
        asyncio.run(consumer.board_event({"payload": payload}))
        return json.loads(consumer.send.call_args.kwargs["text_data"])["data"]

    def test_admin_subscriber_keeps_flag(self):
        self.assertIs(self._send("admin")["is_site_admin"], True)

    def test_site_admin_subscriber_keeps_flag(self):
        self.assertIs(self._send("site_admin", event="member.added")["is_site_admin"], True)

    def test_member_subscriber_loses_flag_and_moderator(self):
        data = self._send("member")
        self.assertNotIn("is_site_admin", data)
        self.assertNotIn("is_moderator", data)
        self.assertEqual(data["role"], "member")

    def test_own_row_subscriber_keeps_moderator_but_not_flag(self):
        data = self._send("viewer", subscriber_id=7)
        self.assertIs(data["is_moderator"], True)
        self.assertNotIn("is_site_admin", data)
