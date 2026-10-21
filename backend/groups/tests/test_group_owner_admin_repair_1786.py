"""Data migration 0022 gives every group's owner a direct admin row (#1786)."""

import importlib

from django.apps import apps
from django.test import TestCase

from accounts.models import User
from groups.models import Group, GroupMembership

migration = importlib.import_module("groups.migrations.0022_restore_group_owner_admin_rows")

ADMIN = GroupMembership.Role.ADMIN


class RestoreOwnerAdminRowsTests(TestCase):
    def _user(self, name, **kw):
        return User.objects.create_user(username=name, password="pw", **kw)

    def _run(self):
        migration.restore_owner_admin_rows(apps, None)

    def _owner_role(self, group):
        return (
            GroupMembership.objects.filter(group=group, user_id=group.owner_id)
            .values_list("role", flat=True).first()
        )

    def test_missing_owner_row_is_created_as_admin(self):
        owner = self._user("o_missing")
        group = Group.objects.create(name="Missing", owner=owner)
        self._run()
        self.assertEqual(self._owner_role(group), ADMIN)
        self.assertIsNotNone(GroupMembership.objects.get(group=group, user=owner).joined_at)

    def test_demoted_owner_row_is_promoted_and_keeps_its_join_date(self):
        owner = self._user("o_demoted")
        group = Group.objects.create(name="Demoted", owner=owner)
        m = GroupMembership.objects.create(group=group, user=owner, role="viewer")
        joined = m.joined_at
        self._run()
        m.refresh_from_db()
        self.assertEqual(m.role, ADMIN)
        self.assertEqual(m.joined_at, joined)

    def test_inactive_owner_gets_the_row_too(self):
        owner = self._user("o_inactive", is_active=False)
        group = Group.objects.create(name="Inactive", owner=owner)
        self._run()
        self.assertEqual(self._owner_role(group), ADMIN)

    def test_healthy_group_and_other_members_untouched(self):
        owner = self._user("o_ok")
        group = Group.objects.create(name="Ok", owner=owner)
        own = GroupMembership.objects.create(group=group, user=owner, role=ADMIN)
        member = GroupMembership.objects.create(group=group, user=self._user("m_ok"), role="member")
        broken = Group.objects.create(name="Broken", owner=owner, parent=group)
        other = GroupMembership.objects.create(group=broken, user=self._user("m_broken"), role="viewer")
        before = {
            m.pk: (m.role, m.joined_at)
            for m in GroupMembership.objects.filter(pk__in=[own.pk, member.pk, other.pk])
        }
        self._run()
        after = {
            m.pk: (m.role, m.joined_at)
            for m in GroupMembership.objects.filter(pk__in=[own.pk, member.pk, other.pk])
        }
        self.assertEqual(before, after)
        self.assertEqual(self._owner_role(broken), ADMIN)
        self.assertEqual(GroupMembership.objects.filter(group=broken).count(), 2)

    def test_idempotent(self):
        owner = self._user("o_twice")
        Group.objects.create(name="Twice", owner=owner)
        self._run()
        rows = list(GroupMembership.objects.order_by("pk").values_list("pk", "group_id", "user_id", "role"))
        self._run()
        self.assertEqual(
            list(GroupMembership.objects.order_by("pk").values_list("pk", "group_id", "user_id", "role")),
            rows,
        )

    def test_reverse_is_a_noop(self):
        operation = migration.Migration.operations[0]
        self.assertIs(operation.reverse_code, operation.noop)
