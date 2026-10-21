"""Django admin user delete keeps at least one active admin on every group (#1786).

Deleting a user removes their group memberships (``GroupMembership.user`` is
CASCADE), so the delete applies the group rule the member guards apply to a
single removal or demotion (security invariants Rule 11): a group the targets
actively administer must keep an active admin outside the deleted set. Only
active admins count, the same definition as ``groups.services``'
``counts_toward_admin_minimum``.
"""
import threading
from unittest import mock, skipUnless

from django.contrib import admin as django_admin
from django.db import connection, connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

import accounts.admin as admin_module
from accounts.models import User
from accounts.tests.test_admin_user_delete_guards import _PLAIN_STATIC, _messages, _staff
from boards.tests.lock_wait import assert_blocked_on_lock
from groups.models import Group, GroupMembership
from groups.services import (
    admin_minimum_q,
    counts_toward_admin_minimum,
    groups_left_without_admin,
    lock_membership_for_removal_or_demotion,
)

PW = "pw123456!x"
ADMIN = GroupMembership.Role.ADMIN
MEMBER = GroupMembership.Role.MEMBER


def _user(name, *, active=True):
    user = User.objects.create_user(username=name, password=PW)
    if not active:
        User.objects.filter(pk=user.pk).update(is_active=False)
        user.refresh_from_db()
    return user


def _group(name, owner, *, owner_role=ADMIN):
    group = Group.objects.create(name=name, owner=owner)
    if owner_role is not None:
        GroupMembership.objects.create(group=group, user=owner, role=owner_role)
    return group


class AdminMinimumPredicateTests(TestCase):
    def test_only_an_active_admin_counts(self):
        owner = _user("pred_owner_1786")
        group = _group("Pred", owner)
        inactive = _user("pred_inactive_1786", active=False)
        member = _user("pred_member_1786")
        GroupMembership.objects.create(group=group, user=inactive, role=ADMIN)
        GroupMembership.objects.create(group=group, user=member, role=MEMBER)
        rows = {m.user_id: m for m in GroupMembership.objects.select_related("user").filter(group=group)}
        self.assertTrue(counts_toward_admin_minimum(rows[owner.pk]))
        self.assertFalse(counts_toward_admin_minimum(rows[inactive.pk]))
        self.assertFalse(counts_toward_admin_minimum(rows[member.pk]))
        # The Q form selects exactly the rows the predicate accepts.
        self.assertEqual(
            set(GroupMembership.objects.filter(admin_minimum_q(), group=group).values_list("user_id", flat=True)),
            {owner.pk},
        )
        self.assertEqual(
            list(Group.objects.filter(admin_minimum_q("memberships__"), pk=group.pk).values_list("pk", flat=True)),
            [group.pk],
        )

    def test_groups_left_without_admin(self):
        gone_owner = _user("glwa_owner_1786", active=False)
        a, b = _user("glwa_a_1786"), _user("glwa_b_1786")
        sole = _group("Sole", gone_owner)  # the owner's admin row does not count
        GroupMembership.objects.create(group=sole, user=a, role=ADMIN)
        shared = _group("Shared", gone_owner)
        GroupMembership.objects.create(group=shared, user=a, role=ADMIN)
        GroupMembership.objects.create(group=shared, user=b, role=ADMIN)
        self.assertEqual(groups_left_without_admin([a.pk]), [sole.pk])
        self.assertEqual(groups_left_without_admin([b.pk]), [])
        self.assertEqual(groups_left_without_admin([a.pk, b.pk]), [sole.pk, shared.pk])
        self.assertEqual(groups_left_without_admin([gone_owner.pk]), [])
        self.assertEqual(groups_left_without_admin([]), [])


@_PLAIN_STATIC
class UserDeleteGroupAdminTests(TestCase):
    def setUp(self):
        self.root = _staff("root_1786", superuser=True)
        self.client.force_login(self.root)
        self.changelist = reverse("admin:accounts_user_changelist")
        # A group whose owner is deactivated: its owner admin row does not
        # count, so the active admins below are all that keep it managed.
        self.gone_owner = _user("gone_owner_1786", active=False)
        self.group = _group("Team", self.gone_owner)
        self.admin_a = _user("admin_a_1786")
        GroupMembership.objects.create(group=self.group, user=self.admin_a, role=ADMIN)

    def delete_url(self, user):
        return reverse("admin:accounts_user_delete", args=[user.pk])

    def bulk(self, users, **extra):
        data = {"action": "delete_selected", "_selected_action": [u.pk for u in users], **extra}
        return self.client.post(self.changelist, data, follow=True)

    def assertRefused(self, response, fragment):
        self.assertTrue(any(fragment in m for m in _messages(response)), _messages(response))

    def assertKept(self, *users):
        self.assertEqual(User.objects.filter(pk__in=[u.pk for u in users]).count(), len(users))
        self.assertEqual(
            GroupMembership.objects.filter(group=self.group, user__in=users, role=ADMIN).count(),
            len(users),
        )

    def test_last_active_admin_refused_on_get_and_post(self):
        for method in ("get", "post"):
            r = getattr(self.client, method)(
                self.delete_url(self.admin_a), {"post": "yes"} if method == "post" else {},
                follow=True,
            )
            self.assertRefused(r, "Cannot delete the last active admin of a group (groups: 1)")
        self.assertKept(self.admin_a)

    def test_allowed_when_another_active_admin_remains(self):
        b = _user("admin_b_1786")
        GroupMembership.objects.create(group=self.group, user=b, role=ADMIN)
        self.client.post(self.delete_url(self.admin_a), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=self.admin_a.pk).exists())
        self.assertKept(b)

    def test_allowed_when_the_active_owner_remains_an_admin(self):
        owner = _user("live_owner_1786")
        group = _group("Live", owner)
        c = _user("admin_c_1786")
        GroupMembership.objects.create(group=group, user=c, role=ADMIN)
        GroupMembership.objects.create(group=self.group, user=c, role=MEMBER)
        self.client.post(self.delete_url(c), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=c.pk).exists())

    def test_inactive_admin_does_not_count_so_its_delete_is_allowed(self):
        idle = _user("idle_admin_1786", active=False)
        lonely = _group("Lonely", self.gone_owner)
        GroupMembership.objects.create(group=lonely, user=idle, role=ADMIN)
        self.client.post(self.delete_url(idle), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=idle.pk).exists())

    def test_member_of_the_group_is_deleted(self):
        m = _user("plain_member_1786")
        GroupMembership.objects.create(group=self.group, user=m, role=MEMBER)
        self.client.post(self.delete_url(m), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=m.pk).exists())

    def test_group_whose_owner_has_no_admin_row(self):
        owner = _user("rowless_owner_1786")
        group = _group("Rowless", owner, owner_role=None)
        d = _user("admin_d_1786")
        GroupMembership.objects.create(group=group, user=d, role=ADMIN)
        r = self.client.post(self.delete_url(d), {"post": "yes"}, follow=True)
        self.assertRefused(r, "last active admin of a group")
        self.assertTrue(User.objects.filter(pk=d.pk).exists())

    def test_bulk_every_active_admin_refused_as_a_set(self):
        b = _user("admin_b2_1786")
        GroupMembership.objects.create(group=self.group, user=b, role=ADMIN)
        for extra in ({}, {"post": "yes"}):
            r = self.bulk([self.admin_a, b], **extra)
            self.assertRefused(r, "Cannot delete every active admin of a group (groups: 1)")
            self.assertRefused(r, "No accounts were deleted")
        self.assertKept(self.admin_a, b)

    def test_bulk_one_of_two_admins_allowed(self):
        b = _user("admin_b3_1786")
        GroupMembership.objects.create(group=self.group, user=b, role=ADMIN)
        self.bulk([b], post="yes")
        self.assertFalse(User.objects.filter(pk=b.pk).exists())
        self.assertKept(self.admin_a)

    def test_refused_under_the_lock_when_the_other_admin_left_after_the_first_check(self):
        # The confirmation page passed with two admins; the other one is
        # demoted before the confirming POST takes its locks.
        b = _user("admin_b4_1786")
        membership = GroupMembership.objects.create(group=self.group, user=b, role=ADMIN)
        real = admin_module.user_deletion_refusals
        calls = {"n": 0}

        def side_effect(targets, actor):
            calls["n"] += 1
            if calls["n"] == 1:
                out = real(targets, actor)
                GroupMembership.objects.filter(pk=membership.pk).update(role=MEMBER)
                return out
            return real(targets, actor)

        with mock.patch("accounts.admin.user_deletion_refusals", side_effect=side_effect):
            r = self.client.post(self.delete_url(self.admin_a), {"post": "yes"}, follow=True)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertRefused(r, "last active admin of a group")
        self.assertKept(self.admin_a)


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class UserDeleteGroupAdminLockTests(TransactionTestCase):
    """The delete and a membership change on the same group queue on the group row."""

    WAIT = 10.0

    def setUp(self):
        self.actor = User.objects.create_superuser(
            username="lk_root_1786", password=PW, email="lk_root_1786@example.com"
        )
        self.gone_owner = _user("lk_owner_1786", active=False)
        self.group = _group("Locked", self.gone_owner)
        self.a, self.b = _user("lk_a_1786"), _user("lk_b_1786")
        for u in (self.a, self.b):
            GroupMembership.objects.create(group=self.group, user=u, role=ADMIN)

    def _delete_thread(self, results):
        def run():
            try:
                request = RequestFactory().post("/")
                request.user = User.objects.get(pk=self.actor.pk)
                django_admin.site._registry[User].delete_model(
                    request, User.objects.get(pk=self.a.pk)
                )
                results["deleter"] = "deleted"
            except admin_module.DeletionRefused as exc:
                results["deleter"] = ("refused", exc.messages)
            except Exception as exc:
                results["deleter"] = exc
            finally:
                connections.close_all()

        return threading.Thread(target=run, name="deleter")

    def test_delete_waits_for_a_demotion_and_then_refuses(self):
        locked, release = threading.Event(), threading.Event()
        demoted = {}

        def demoter():
            try:
                with transaction.atomic():
                    m = lock_membership_for_removal_or_demotion(
                        self.group.pk, self.b.pk, new_role=MEMBER
                    )
                    m.role = MEMBER
                    m.save(update_fields=["role"])
                    locked.set()
                    release.wait(self.WAIT)
                demoted["ok"] = True
            except Exception as exc:
                demoted["err"] = exc
            finally:
                locked.set()
                connections.close_all()

        results = {}
        d = threading.Thread(target=demoter)
        d.start()
        self.assertTrue(locked.wait(self.WAIT))
        deleter = self._delete_thread(results)
        deleter.start()
        try:
            assert_blocked_on_lock(self, deleter, "the user delete must wait for the demotion's group lock")
        finally:
            release.set()
            d.join(self.WAIT)
            deleter.join(self.WAIT)
        self.assertEqual(demoted, {"ok": True})
        outcome = results["deleter"]
        self.assertIsInstance(outcome, tuple, outcome)
        self.assertEqual(outcome[0], "refused")
        self.assertTrue(any("last active admin of a group" in m for m in outcome[1]), outcome)
        self.assertTrue(User.objects.filter(pk=self.a.pk).exists())

    def test_membership_change_waits_for_the_delete(self):
        paused, release = threading.Event(), threading.Event()
        model_admin = django_admin.site._registry[User]
        real = type(model_admin)._lock_cards_then_targets

        def hold_after_statement_1(admin_self, request, pks):
            paused.set()
            release.wait(self.WAIT)
            return real(admin_self, request, pks)

        changed = {}

        def demoter():
            try:
                with transaction.atomic():
                    lock_membership_for_removal_or_demotion(self.group.pk, self.b.pk, new_role=MEMBER)
                changed["done"] = True
            except Exception as exc:  # a refusal is fine; only the wait is asserted
                changed["done"] = exc
            finally:
                connections.close_all()

        results = {}
        with mock.patch.object(
            type(model_admin), "_lock_cards_then_targets", hold_after_statement_1
        ):
            deleter = self._delete_thread(results)
            deleter.start()
            self.assertTrue(paused.wait(self.WAIT))
            d = threading.Thread(target=demoter)
            d.start()
            try:
                assert_blocked_on_lock(self, d, "the demotion must wait for the user delete's group lock")
            finally:
                release.set()
                deleter.join(self.WAIT)
                d.join(self.WAIT)
        self.assertEqual(results["deleter"], "deleted")
        self.assertIn("done", changed)
        self.assertFalse(User.objects.filter(pk=self.a.pk).exists())
