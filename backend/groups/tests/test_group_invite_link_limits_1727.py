"""Shareable group invite links cannot grant admin and always expire (#1727).

A shareable link is a bearer credential: whoever holds it gets its grant. A
group admin inherits board admin on every board in the subtree, so the link
create endpoint refuses ``admin`` and refuses a never-expiring lifetime, the
same rule board invite links follow. Emailed invites, which are bound to one
recipient address (#1678), still grant admin. Validation is at creation only:
links minted before the change keep working until they expire or are revoked.
"""
import re
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from groups.models import Group, GroupInviteLink, GroupMembership
from groups.serializers import (
    GROUP_INVITE_LINK_DEFAULT_EXPIRY_DAYS,
    GROUP_INVITE_LINK_MAX_EXPIRY_DAYS,
    GroupInviteLinkCreateSerializer,
)

TOKEN_RE = re.compile(r"vbng_[0-9a-f]{40}")


class _Fixture:
    def make_group(self):
        cache.clear()
        self.admin = User.objects.create_user(username="gadmin", password="p", email="gadmin@acme.test")
        self.group = Group.objects.create(name="Platform Team", owner=self.admin)
        GroupMembership.objects.create(group=self.group, user=self.admin, role=GroupMembership.Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f"/api/v1/groups/{self.group.pk}/invite-links/"

    def create(self, **body):
        return self.client.post(self.url, body, format="json")

    def join(self, raw, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(f"/api/v1/groups/join/{raw}/")

    def assert_days_from_now(self, expires_at, days):
        self.assertIsNotNone(expires_at)
        self.assertAlmostEqual(
            (expires_at - timezone.now()).total_seconds(), timedelta(days=days).total_seconds(), delta=60,
        )


class GroupInviteLinkRoleTests(_Fixture, TestCase):

    def setUp(self):
        self.make_group()

    def tearDown(self):
        cache.clear()

    def test_admin_role_is_refused_on_link_create(self):
        r = self.create(role="admin", expiry_days=7)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
        self.assertIn("role", r.json())
        self.assertFalse(GroupInviteLink.objects.filter(group=self.group).exists())

    def test_every_non_admin_role_is_accepted(self):
        for role in ("member", "collaborator", "viewer"):
            with self.subTest(role=role):
                r = self.create(role=role)
                self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
                self.assertEqual(r.json()["role"], role)
                GroupInviteLink.objects.filter(pk=r.json()["id"]).update(is_active=False)

    def test_generate_refuses_a_shareable_admin_link(self):
        with self.assertRaises(ValueError):
            GroupInviteLink.generate(group=self.group, created_by=self.admin, role=GroupInviteLink.Role.ADMIN)
        self.assertFalse(GroupInviteLink.objects.filter(group=self.group).exists())

    def test_serializer_role_choices_exclude_admin_only(self):
        choices = set(GroupInviteLinkCreateSerializer().fields["role"].choices)
        self.assertEqual(choices, set(GroupInviteLink.Role.values) - {GroupInviteLink.Role.ADMIN})


class GroupInviteLinkExpiryTests(_Fixture, TestCase):

    def setUp(self):
        self.make_group()

    def tearDown(self):
        cache.clear()

    def test_omitted_expiry_defaults_to_seven_days(self):
        self.assertEqual(GROUP_INVITE_LINK_DEFAULT_EXPIRY_DAYS, 7)
        r = self.create()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        link = GroupInviteLink.objects.get(pk=r.json()["id"])
        self.assert_days_from_now(link.expires_at, 7)
        self.assertIsNotNone(r.json()["expires_at"])

    def test_explicit_null_expiry_is_refused(self):
        r = self.create(expiry_days=None)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
        self.assertIn("expiry_days", r.json())
        self.assertFalse(GroupInviteLink.objects.filter(group=self.group).exists())

    def test_expiry_over_thirty_days_is_refused(self):
        self.assertEqual(GROUP_INVITE_LINK_MAX_EXPIRY_DAYS, 30)
        for value in (31, 365):
            with self.subTest(value=value):
                r = self.create(expiry_days=value)
                self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
                self.assertIn("expiry_days", r.json())
        self.assertFalse(GroupInviteLink.objects.filter(group=self.group).exists())

    def test_zero_and_negative_expiry_are_refused(self):
        for value in (0, -1):
            with self.subTest(value=value):
                r = self.create(expiry_days=value)
                self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)

    def test_any_value_from_one_to_thirty_is_accepted(self):
        # Pre-#1727 clients could send any positive day count; 1..30 stays valid.
        for value in (1, 14, 30):
            with self.subTest(value=value):
                r = self.create(expiry_days=value)
                self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
                link = GroupInviteLink.objects.get(pk=r.json()["id"])
                self.assert_days_from_now(link.expires_at, value)
                link.is_active = False
                link.save(update_fields=["is_active"])


class GroupInviteLinkNoUpdatePathTests(_Fixture, TestCase):
    """No route can raise an existing link's role or clear its expiry."""

    def setUp(self):
        self.make_group()

    def tearDown(self):
        cache.clear()

    def test_link_detail_route_refuses_patch_and_put(self):
        r = self.create(role="viewer")
        link_id = r.json()["id"]
        for method in ("patch", "put"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(
                    f"{self.url}{link_id}/", {"role": "admin", "expiry_days": None}, format="json",
                )
                self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, resp.content)
        link = GroupInviteLink.objects.get(pk=link_id)
        self.assertEqual(link.role, "viewer")
        self.assertIsNotNone(link.expires_at)

    def test_list_route_refuses_patch_and_put(self):
        for method in ("patch", "put"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(self.url, {"role": "admin"}, format="json")
                self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, resp.content)


class PreExistingGroupInviteLinkTests(_Fixture, TestCase):
    """Links minted before #1727 are not touched: no migration, they still redeem."""

    def setUp(self):
        self.make_group()
        self.joiner = User.objects.create_user(username="joiner", password="p", email="joiner@acme.test")

    def tearDown(self):
        cache.clear()

    def legacy_admin_link(self):
        # generate() now refuses a shareable admin link, so write the role the
        # way a pre-#1727 row holds it.
        link, raw = GroupInviteLink.generate(group=self.group, created_by=self.admin, expires_at=None)
        GroupInviteLink.objects.filter(pk=link.pk).update(role=GroupInviteLink.Role.ADMIN)
        link.refresh_from_db()
        return link, raw

    def test_existing_admin_never_expiring_link_still_redeems(self):
        link, raw = self.legacy_admin_link()
        r = self.join(raw, self.joiner)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        membership = GroupMembership.objects.get(group=self.group, user=self.joiner)
        self.assertEqual(membership.role, GroupMembership.Role.ADMIN)

    def test_existing_link_still_lists_with_its_admin_role_and_no_expiry(self):
        link, _raw = self.legacy_admin_link()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        row = next(item for item in r.json() if item["id"] == link.pk)
        self.assertEqual(row["role"], "admin")
        self.assertIsNone(row["expires_at"])
        self.assertEqual(row["status"], "pending")


@override_settings(
    INVITE_EMAIL_ENABLED=True,
    DEMO_MODE=False,
    FRONTEND_URL="https://kanban.acme.test",
    DEFAULT_FROM_EMAIL="invites@acme.test",
)
class EmailedGroupInviteStillGrantsAdminTests(_Fixture, TestCase):
    """The email channel is unchanged and remains the way to grant admin."""

    INVITED = "new.admin@example.org"

    def setUp(self):
        self.make_group()
        self.invitee = User.objects.create_user(username="newadmin", password="p", email=self.INVITED)

    def tearDown(self):
        cache.clear()

    def test_emailed_invite_grants_admin(self):
        r = self.client.post(
            f"{self.url}send/", {"email": self.INVITED, "role": "admin"}, format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_202_ACCEPTED, r.content)
        link = GroupInviteLink.objects.get(group=self.group, delivery=GroupInviteLink.Delivery.EMAIL)
        self.assertEqual(link.role, GroupInviteLink.Role.ADMIN)
        self.assert_days_from_now(link.expires_at, 7)

        self.assertEqual(len(mail.outbox), 1)
        raw = TOKEN_RE.search(mail.outbox[0].body).group(0)
        joined = self.join(raw, self.invitee)
        self.assertEqual(joined.status_code, status.HTTP_201_CREATED, joined.content)
        membership = GroupMembership.objects.get(group=self.group, user=self.invitee)
        self.assertEqual(membership.role, GroupMembership.Role.ADMIN)
