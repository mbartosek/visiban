"""Data migration 0023 revokes or caps pre-#1727 shareable group links (#1940)."""

import importlib
from datetime import timedelta

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from groups.models import Group, GroupInviteLink

migration = importlib.import_module("groups.migrations.0023_revoke_legacy_group_invite_links")


class RevokeLegacyGroupInviteLinksTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="o_1940", password="pw", email="o1940@x.test")
        self.group = Group.objects.create(name="G1940", owner=self.owner)
        self.n = 0

    def _link(self, **kw):
        self.n += 1
        fields = dict(
            group=self.group, created_by=self.owner, token_hash=f"h1940-{self.n}",
            prefix="vbng_x", role="member", delivery="link", is_active=True, expires_at=None,
        )
        fields.update(kw)
        return GroupInviteLink.objects.create(**fields)

    def _run(self):
        migration.revoke_legacy_group_invite_links(apps, None)

    def test_active_unredeemed_admin_link_is_revoked_not_downgraded(self):
        link = self._link(role="admin", expires_at=timezone.now() + timedelta(days=3))
        self._run()
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        self.assertEqual(link.role, "admin")

    def test_never_expiring_link_gets_seven_day_expiry(self):
        link = self._link(role="member")
        before = timezone.now()
        self._run()
        link.refresh_from_db()
        self.assertTrue(link.is_active)
        self.assertGreaterEqual(link.expires_at, before + timedelta(days=7))
        self.assertLessEqual(link.expires_at, timezone.now() + timedelta(days=7))

    def test_admin_never_expiring_link_is_revoked_and_not_given_expiry(self):
        link = self._link(role="admin")
        self._run()
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        self.assertIsNone(link.expires_at)

    def test_emailed_invites_are_untouched(self):
        email_admin = self._link(delivery="email", role="admin")
        email_open = self._link(delivery="email", role="member")
        self._run()
        for link in (email_admin, email_open):
            link.refresh_from_db()
            self.assertTrue(link.is_active)
            self.assertIsNone(link.expires_at)
        email_admin.refresh_from_db()
        self.assertEqual(email_admin.role, "admin")

    def test_already_expiring_member_link_is_untouched(self):
        expiry = timezone.now() + timedelta(days=20)
        link = self._link(role="member", expires_at=expiry)
        self._run()
        link.refresh_from_db()
        self.assertTrue(link.is_active)
        self.assertEqual(link.expires_at, expiry)

    def test_redeemed_and_inactive_rows_are_untouched(self):
        used = self._link(role="admin", single_use=True, used_at=timezone.now())
        used_open = self._link(role="member", single_use=True, used_at=timezone.now())
        revoked = self._link(role="member", is_active=False)
        self._run()
        used.refresh_from_db()
        self.assertTrue(used.is_active)
        used_open.refresh_from_db()
        self.assertIsNone(used_open.expires_at)
        revoked.refresh_from_db()
        self.assertIsNone(revoked.expires_at)

    def test_rerun_changes_nothing(self):
        link = self._link(role="member")
        self._run()
        link.refresh_from_db()
        first = link.expires_at
        self._run()
        link.refresh_from_db()
        self.assertEqual(link.expires_at, first)
