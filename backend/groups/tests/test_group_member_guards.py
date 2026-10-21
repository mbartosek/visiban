"""Membership removal and demotion keep the group's owner and last admin in place.

``PATCH``/``DELETE /api/v1/groups/{id}/members/{user_id}/`` refuse to remove or
demote the group owner, and refuse to remove or demote the group's last admin.
The owner leaves through ownership transfer or by deleting the group.
"""

import ast
import threading
from pathlib import Path
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import SiteSetting, User
from boards.tests.lock_wait import assert_blocked_on_lock
from groups.models import Group, GroupMembership
from groups.tests.test_group_invite_vs_group_delete import _GroupDeleteRaceBase
from groups.services import LAST_ADMIN_MESSAGE, OWNER_MEMBERSHIP_MESSAGE

ADMIN = GroupMembership.Role.ADMIN
MEMBER = GroupMembership.Role.MEMBER


def _url(group, user):
    return f"/api/v1/groups/{group.pk}/members/{user.pk}/"


class MemberGuardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="gowner", password="pw")
        self.admin2 = User.objects.create_user(username="gadmin2", password="pw")
        self.member = User.objects.create_user(username="gmember", password="pw")
        self.group = Group.objects.create(name="Team", owner=self.owner)
        GroupMembership.objects.create(group=self.group, user=self.owner, role=ADMIN)
        GroupMembership.objects.create(group=self.group, user=self.admin2, role=ADMIN)
        GroupMembership.objects.create(group=self.group, user=self.member, role=MEMBER)
        self.client = APIClient()
        self.client.force_authenticate(self.admin2)

    def _role(self, user):
        return GroupMembership.objects.get(group=self.group, user=user).role

    def test_owner_removal_refused(self):
        r = self.client.delete(_url(self.group, self.owner))
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json()["detail"], OWNER_MEMBERSHIP_MESSAGE)
        self.assertTrue(GroupMembership.objects.filter(group=self.group, user=self.owner).exists())

    def test_owner_demotion_refused(self):
        r = self.client.patch(_url(self.group, self.owner), {"role": "member"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json()["detail"], OWNER_MEMBERSHIP_MESSAGE)
        self.assertEqual(self._role(self.owner), ADMIN)

    def test_owner_set_to_admin_is_accepted(self):
        r = self.client.patch(_url(self.group, self.owner), {"role": "admin"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_last_admin_removal_refused(self):
        # A non-owner is the only admin: the owner's row is a plain member.
        GroupMembership.objects.filter(group=self.group, user=self.owner).update(role=MEMBER)
        r = self.client.delete(_url(self.group, self.admin2))
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json()["detail"], LAST_ADMIN_MESSAGE)
        self.assertEqual(self._role(self.admin2), ADMIN)

    def test_last_admin_demotion_refused(self):
        GroupMembership.objects.filter(group=self.group, user=self.owner).update(role=MEMBER)
        r = self.client.patch(_url(self.group, self.admin2), {"role": "viewer"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json()["detail"], LAST_ADMIN_MESSAGE)
        self.assertEqual(self._role(self.admin2), ADMIN)

    def test_deactivated_admin_does_not_count_toward_the_minimum(self):
        # The owner (an admin) is deactivated, so admin2 is the only active admin.
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        r = self.client.delete(_url(self.group, self.admin2))
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r.json()["detail"], LAST_ADMIN_MESSAGE)
        r = self.client.patch(_url(self.group, self.admin2), {"role": "member"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._role(self.admin2), ADMIN)

    def test_deactivated_admin_is_still_removable(self):
        # Removing a deactivated admin cannot reduce the active-admin count.
        User.objects.filter(pk=self.admin2.pk).update(is_active=False)
        self.client.force_authenticate(self.owner)
        r = self.client.delete(_url(self.group, self.admin2))
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)

    def test_active_admin_removable_while_another_active_admin_remains(self):
        extra = User.objects.create_user(username="gextra", password="pw")
        GroupMembership.objects.create(group=self.group, user=extra, role=ADMIN)
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        r = self.client.delete(_url(self.group, extra))
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)

    def test_non_last_admin_removable_and_demotable(self):
        r = self.client.patch(_url(self.group, self.admin2), {"role": "member"}, format="json")
        # admin2 demotes itself while the owner remains an admin.
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(self.owner)
        GroupMembership.objects.filter(group=self.group, user=self.admin2).update(role=ADMIN)
        r = self.client.delete(_url(self.group, self.admin2))
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)

    def test_regular_member_still_removable(self):
        r = self.client.delete(_url(self.group, self.member))
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)

    def test_owner_removable_after_transfer(self):
        self.client.force_authenticate(self.owner)
        r = self.client.post(
            f"/api/v1/groups/{self.group.pk}/transfer-ownership/",
            {"new_owner_id": self.admin2.pk, "confirmation": "Team"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(self.admin2)
        r = self.client.delete(_url(self.group, self.owner))
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)

    def test_missing_membership_still_404(self):
        outsider = User.objects.create_user(username="gout", password="pw")
        r = self.client.delete(_url(self.group, outsider))
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class ConcurrentAdminRemovalTests(TransactionTestCase):
    """Two admins removed by different requests at once: exactly one succeeds."""

    def test_two_admins_removed_concurrently_leaves_one(self):
        a = User.objects.create_user(username="ga", password="pw")
        b = User.objects.create_user(username="gb", password="pw")
        # The owner row is a plain member so only a and b count as admins.
        owner = User.objects.create_user(username="go", password="pw")
        group = Group.objects.create(name="Duo", owner=owner)
        GroupMembership.objects.create(group=group, user=owner, role=MEMBER)
        GroupMembership.objects.create(group=group, user=a, role=ADMIN)
        GroupMembership.objects.create(group=group, user=b, role=ADMIN)

        barrier = threading.Barrier(2)
        results = {}

        def worker(actor, target):
            try:
                client = APIClient()
                client.force_authenticate(actor)
                barrier.wait(timeout=10)
                results[target.pk] = client.delete(_url(group, target)).status_code
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(a, b)),
            threading.Thread(target=worker, args=(b, a)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        codes = sorted(results.values())
        self.assertEqual(codes, [204, 400], results)
        self.assertEqual(
            GroupMembership.objects.filter(group=group, role=ADMIN).count(), 1
        )

    def test_two_active_admins_removed_concurrently_with_a_deactivated_admin(self):
        a = User.objects.create_user(username="gia", password="pw")
        b = User.objects.create_user(username="gib", password="pw")
        idle = User.objects.create_user(username="gii", password="pw", is_active=False)
        owner = User.objects.create_user(username="gio", password="pw")
        group = Group.objects.create(name="Trio", owner=owner)
        GroupMembership.objects.create(group=group, user=owner, role=MEMBER)
        GroupMembership.objects.create(group=group, user=idle, role=ADMIN)
        GroupMembership.objects.create(group=group, user=a, role=ADMIN)
        GroupMembership.objects.create(group=group, user=b, role=ADMIN)

        barrier = threading.Barrier(2)
        results = {}

        def worker(actor, target):
            try:
                client = APIClient()
                client.force_authenticate(actor)
                barrier.wait(timeout=10)
                results[target.pk] = client.delete(_url(group, target)).status_code
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(a, b)),
            threading.Thread(target=worker, args=(b, a)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # Three admin rows but only two are active: exactly one removal may succeed.
        self.assertEqual(sorted(results.values()), [204, 400], results)
        self.assertEqual(
            GroupMembership.objects.filter(group=group, role=ADMIN, user__is_active=True).count(), 1
        )


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class MemberChangeVsOwnershipTransferTests(_GroupDeleteRaceBase):
    """A demotion that queues behind an ownership transfer sees the new owner."""

    COMMIT_HOLD = (transaction, "on_commit", False)

    def setUp(self):
        super().setUp()
        # The middleware creates the SiteSetting row on first use and that save
        # registers an on_commit: create it now so the hold point is the
        # transfer's own callback.
        SiteSetting.get()
        self.heir = User.objects.create_user(username="h", password="p", email="h@example.com")
        GroupMembership.objects.create(group=self.group, user=self.heir, role=ADMIN)

    def _client(self):
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=self.sender.pk))
        return client

    def _transfer(self):
        return self._client().post(
            f"/api/v1/groups/{self.group_id}/transfer-ownership/",
            {"new_owner_id": self.heir.pk, "confirmation": "G1638"}, format="json",
        )

    def _demote_heir(self):
        return self._client().patch(
            f"/api/v1/groups/{self.group_id}/members/{self.heir.pk}/", {"role": "member"}, format="json",
        )

    def test_demotion_behind_transfer_is_refused_for_the_new_owner(self):
        transferred, demoted = self._race(self.COMMIT_HOLD, self._transfer, self._demote_heir)
        self.assertEqual(transferred.status_code, 200, transferred.content[:300])
        self.assertEqual(demoted.status_code, 400, demoted.content[:300])
        self.assertEqual(demoted.json()["detail"], OWNER_MEMBERSHIP_MESSAGE)
        self.assertEqual(Group.objects.get(pk=self.group_id).owner_id, self.heir.pk)
        self.assertEqual(
            GroupMembership.objects.get(group_id=self.group_id, user=self.heir).role, ADMIN
        )


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class MemberRemovalQueueingTests(MemberChangeVsOwnershipTransferTests):
    """A removal that queues behind another write finds the target gone and answers 404."""

    DELETE_HOLD = (Group, "delete", True)
    # Inherited setUp and helpers only; the transfer test runs in its own class.
    test_demotion_behind_transfer_is_refused_for_the_new_owner = None

    def _remove_heir(self):
        return self._client().delete(f"/api/v1/groups/{self.group_id}/members/{self.heir.pk}/")

    def test_removal_behind_removal_of_the_same_member(self):
        first, second = self._race(self.COMMIT_HOLD, self._remove_heir, self._remove_heir)
        self.assertEqual(first.status_code, 204, first.content[:300])
        self.assertEqual(second.status_code, 404, second.content[:300])

    def test_removal_behind_group_delete(self):
        deleted, removed = self._race(self.DELETE_HOLD, self._delete, self._remove_heir)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(removed.status_code, 404, removed.content[:300])
        self.assertFalse(Group.objects.filter(pk=self.group_id).exists())

    def test_role_change_waits_on_a_locked_admin_row(self):
        """The admin rows are locked too, for writers that skip the group lock."""
        other = User.objects.create_user(username="gother", password="p", email="gother@example.com")
        GroupMembership.objects.create(group=self.group, user=other, role=ADMIN)
        held, release = threading.Event(), threading.Event()

        def hold_row():
            try:
                with transaction.atomic():
                    GroupMembership.objects.select_for_update().get(group_id=self.group_id, user=self.heir)
                    held.set()
                    release.wait(10)
            finally:
                connections.close_all()

        def demote_other():
            try:
                return self._client().patch(
                    f"/api/v1/groups/{self.group_id}/members/{other.pk}/", {"role": "member"}, format="json",
                )
            finally:
                connections.close_all()

        results = {}
        holder = threading.Thread(target=hold_row)
        holder.start()
        self.assertTrue(held.wait(10))
        patcher = threading.Thread(target=lambda: results.setdefault("r", demote_other()))
        patcher.start()
        try:
            assert_blocked_on_lock(self, patcher, "role change did not wait on the locked admin row")
        finally:
            release.set()
            holder.join(10)
            patcher.join(10)
        self.assertEqual(results["r"].status_code, 200, results["r"].content[:300])


class GroupMembershipEntryPointTests(SimpleTestCase):
    """Every function that deletes or demotes a ``GroupMembership`` uses the shared guard.

    Limits: a static scan of ``backend/`` outside tests and migrations. It
    flags ``GroupMembership...delete()/update()/bulk_update()`` call chains and
    ``.role = ...`` assignments, and requires the enclosing function to call
    ``lock_membership_for_removal_or_demotion`` or be allowlisted with a
    reason. It does not see database cascades or a membership reached through a
    variable it cannot trace to the model name.
    """

    GUARD = "lock_membership_for_removal_or_demotion"
    ALLOWLIST = {
        "groups/services.py:transfer_group_ownership": "only promotes to admin",
    }

    @staticmethod
    def _chain_root(node):
        while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
            node = node.value if isinstance(node, (ast.Attribute, ast.Subscript)) else node.func
        return node.id if isinstance(node, ast.Name) else None

    def _violations(self):
        backend = Path(__file__).resolve().parents[2]
        found, flagged = [], {}
        for path in backend.rglob("*.py"):
            rel = path.relative_to(backend).as_posix()
            if "/tests/" in f"/{rel}" or "/migrations/" in rel or rel.startswith(("venv", ".venv")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls_guard = any(
                    isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == self.GUARD
                    for n in ast.walk(fn)
                )
                for n in ast.walk(fn):
                    writes = (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr in {"delete", "update", "bulk_update"}
                        and self._chain_root(n.func.value) == "GroupMembership"
                    ) or (
                        isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Attribute) and t.attr == "role" for t in n.targets)
                        and "groups" in rel
                    )
                    if writes:
                        key = f"{rel}:{fn.name}"
                        flagged[key] = calls_guard
                        if not calls_guard and key not in self.ALLOWLIST:
                            found.append(key)
        return found, flagged

    def test_every_membership_removal_or_demotion_uses_the_guard(self):
        found, flagged = self._violations()
        self.assertEqual(found, [], "unguarded membership writes: " + ", ".join(found))
        self.assertIn("groups/views.py:update_member", flagged)

    def test_allowlist_has_no_stale_entries(self):
        _, flagged = self._violations()
        self.assertEqual(sorted(set(self.ALLOWLIST) - set(flagged)), [])
