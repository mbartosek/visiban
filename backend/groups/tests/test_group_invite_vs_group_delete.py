"""Group invite redemption racing a group delete (#1638).

The group side of #1631. A redemption — the join view, invite-only
registration, or the grant that runs when an invited address is confirmed
(``redeem_reserved_group_invites``, #1704) — inserts the new member's
``GroupMembership`` while it holds the invite row. ``GroupViewSet.destroy``
took no lock before ``group.delete()``: the collector reads the group's
memberships, then waits on the invite row at its ``DELETE``, so when the
redemption committed the delete failed its deferred foreign key at COMMIT (an
``IntegrityError``, a 500), or deadlocked against a grant that holds the group
row and waits on the invite.

The delete now locks the group row and its subgroups (``FOR UPDATE``) first,
and every redemption locks the group row (``FOR NO KEY UPDATE``) before the
invite row, so the two queue whole in either order:

* redemption first: it commits, then the delete removes the group and the new
  membership with it (204);
* delete first: the redemption finds the group gone and answers 404 (join),
  ``invite_invalid`` (registration, a 400), or grants nothing (confirmation),
  with nothing written and nothing broadcast.

Each race pauses the first request inside its transaction at a point where it
holds its locks, starts the second, proves the second is waiting on a
PostgreSQL lock (``boards.tests.lock_wait``), and releases. PostgreSQL only:
SQLite has no row locks. ``GroupGoneUnitTests`` runs everywhere.
"""

import logging
import threading
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.invite_utils import InviteTokenError
from accounts.models import SiteSetting, User
from boards.tests.invite_fixtures import generate_invite
from boards.tests.lock_wait import wait_until_blocked_on_lock
from groups import invite_registration as _reg
from groups import views as _views
from groups.invite_registration import (
    lock_invite_group,
    redeem_group_registration_token,
    redeem_reserved_group_invites,
)
from groups.models import Group, GroupInviteLink, GroupMembership
from groups.services import lock_group_subtree_for_delete

HOLD_TIMEOUT = 10.0
PASSWORD = "Sup3rS3cr3t!xyz"
INVITED = "invitee1638@example.org"
REGISTERED = "new1638@example.com"


def _set_mode(mode):
    row = SiteSetting.get()
    row.registration_mode = mode
    row.save()
    cache.clear()  # drop any cached registration mode



def _checkpoint():
    """A hold point for a holder that is not a request (patched by ``_race``)."""


def _this_module():
    import sys

    return sys.modules[__name__]


class GroupGoneUnitTests(TestCase):
    """The service-side group lock answers False, and redemption refuses, once the group is gone."""

    def setUp(self):
        self.sender = User.objects.create_user(username="s1638u", password="p", is_site_admin=True)
        self.group = Group.objects.create(name="U1638", owner=self.sender)
        GroupMembership.objects.create(group=self.group, user=self.sender, role=GroupMembership.Role.ADMIN)

    def test_lock_invite_group_present_and_missing(self):
        with transaction.atomic():
            self.assertTrue(lock_invite_group(self.group.pk))
            self.assertFalse(lock_invite_group(self.group.pk + 10_000))

    def test_subtree_lock_reads_the_locked_row_and_misses_a_gone_group(self):
        child = Group.objects.create(name="C1638u", owner=self.sender, parent=self.group)
        with transaction.atomic():
            self.assertEqual(lock_group_subtree_for_delete(self.group.pk), (self.sender.pk, None))
            self.assertEqual(lock_group_subtree_for_delete(child.pk), (self.sender.pk, self.group.pk))
            self.assertIsNone(lock_group_subtree_for_delete(self.group.pk + 10_000))

    def test_registration_redeem_refuses_a_group_deleted_since_validation(self):
        link, _raw = generate_invite(
            GroupInviteLink, group=self.group, created_by=self.sender, role=GroupInviteLink.Role.MEMBER,
            expires_at=timezone.now() + timedelta(days=7), single_use=True,
            delivery=GroupInviteLink.Delivery.EMAIL,
        )
        newcomer = User.objects.create_user(username="n1638u", password="p", email="n1638u@example.com")
        Group.objects.filter(pk=self.group.pk).delete()
        with self.assertRaises(InviteTokenError) as ctx:
            redeem_group_registration_token(link, newcomer)
        self.assertEqual(ctx.exception.code, "invite_invalid")
        self.assertFalse(GroupMembership.objects.filter(user=newcomer).exists())


class _ErrorRecords(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class _GroupDeleteRaceBase(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.sender = User.objects.create_user(
            username="s1638", password="p", email="s1638@example.com", is_site_admin=True,
        )
        self.joiner = User.objects.create_user(username="j1638", password="p", email="j1638@example.com")
        self.group = Group.objects.create(name="G1638", owner=self.sender)
        GroupMembership.objects.create(group=self.group, user=self.sender, role=GroupMembership.Role.ADMIN)
        self.group_id = self.group.pk
        self.target_group = self.group
        self.make_link()
        patcher = patch("groups.broadcast.broadcast_group_event")
        self.broadcast = patcher.start()
        self.addCleanup(patcher.stop)
        self.errors = _ErrorRecords()
        logging.getLogger().addHandler(self.errors)
        self.addCleanup(logging.getLogger().removeHandler, self.errors)
        self.addCleanup(cache.clear)

    def make_link(self, **kwargs):
        self.link, self.raw = generate_invite(
            GroupInviteLink, group=self.target_group, created_by=self.sender,
            role=GroupInviteLink.Role.MEMBER, expires_at=timezone.now() + timedelta(days=7),
            single_use=True, delivery=GroupInviteLink.Delivery.EMAIL, **kwargs,
        )

    # -- requests --------------------------------------------------------

    def _delete(self):
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=self.sender.pk))
        return client.delete(f"/api/v1/groups/{self.group_id}/")

    def _join(self):
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=self.joiner.pk))
        return client.post(f"/api/v1/groups/join/{self.raw}/")

    def _register(self):
        return APIClient().post("/api/v1/auth/registration/", {
            "email": REGISTERED, "password1": PASSWORD, "password2": PASSWORD,
            "invite_token": self.raw,
        })

    # -- harness ---------------------------------------------------------

    def _thread(self, name, fn, results):
        def run():
            try:
                results[name] = fn()
            except Exception as exc:  # surfaced by the assertions
                results[name] = exc
            finally:
                # Release this thread's DB connection, or teardown cannot DROP
                # the test database (backend CLAUDE.md, threaded tests).
                connections.close_all()

        t = threading.Thread(target=run, name=name)
        t.start()
        return t

    def _race(self, hold, first, second):
        """Run ``first`` (thread "holder") until it reaches ``hold``, start
        ``second``, require that it waits on a lock, then release both.

        ``hold`` is ``(owner, attr, before)``: the holder pauses before
        (``before=True``) or after calling ``owner.attr``.
        """
        owner, attr, before = hold
        real = getattr(owner, attr)
        paused, release, results = threading.Event(), threading.Event(), {}

        def pause():
            if threading.current_thread().name == "holder" and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)

        def wrapper(*args, **kwargs):
            if before:
                pause()
            out = real(*args, **kwargs)
            if not before:
                pause()
            return out

        # autospec keeps ``self`` for a method patched on a class (Group.delete).
        with patch.object(owner, attr, autospec=isinstance(owner, type), side_effect=wrapper):
            holder = self._thread("holder", first, results)
            self.assertTrue(paused.wait(HOLD_TIMEOUT), "first request never reached its hold point")
            second_t = self._thread("second", second, results)
            waited = wait_until_blocked_on_lock(second_t)
            release.set()
            holder.join(HOLD_TIMEOUT)
            second_t.join(HOLD_TIMEOUT)
        for name in ("holder", "second"):
            self.assertNotIsInstance(results.get(name), Exception, results.get(name))
        self.assertTrue(waited, f"second request did not wait for the first: {results}")
        return results["holder"], results["second"]

    # Hold points. The delete pauses just before ``group.delete()``: after the
    # group lock the fix adds, before the collector runs. A redemption pauses
    # after its membership insert, still uncommitted and holding its locks.
    DELETE_HOLD = (Group, "delete", True)
    JOIN_HOLD = (_views, "GroupMembershipSerializer", False)
    GRANT_HOLD = (_reg, "_grant_membership", False)

    # -- assertions ------------------------------------------------------

    def _assert_group_gone_cleanly(self):
        self.assertFalse(Group.objects.filter(pk=self.group_id).exists())
        self.assertFalse(Group.objects.filter(pk=self.target_group.pk).exists())
        self.assertFalse(GroupMembership.objects.filter(group_id=self.target_group.pk).exists())
        self.assertFalse(GroupInviteLink.objects.filter(pk=self.link.pk).exists())
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    def _assert_no_member_added(self):
        from groups.broadcast import EVT_GROUP_DELETED

        events = {c.args[1] for c in self.broadcast.call_args_list}
        self.assertLessEqual(events, {EVT_GROUP_DELETED}, events)


class JoinVsGroupDeleteTests(_GroupDeleteRaceBase):
    def test_group_delete_behind_join(self):
        joined, deleted = self._race(self.JOIN_HOLD, self._join, self._delete)
        self.assertEqual(joined.status_code, 201, joined.content[:300])
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self._assert_group_gone_cleanly()

    def test_join_behind_group_delete(self):
        deleted, joined = self._race(self.DELETE_HOLD, self._delete, self._join)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(joined.status_code, 404, joined.content[:300])
        self.assertFalse(GroupMembership.objects.filter(user=self.joiner).exists())
        self._assert_group_gone_cleanly()
        self._assert_no_member_added()

    def test_second_delete_behind_a_delete_answers_404(self):
        first, second = self._race(self.DELETE_HOLD, self._delete, self._delete)
        self.assertEqual(first.status_code, 204, first.content[:300])
        self.assertEqual(second.status_code, 404, second.content[:300])
        self._assert_group_gone_cleanly()
        from groups.broadcast import EVT_GROUP_DELETED

        deleted = [c for c in self.broadcast.call_args_list if c.args[1] == EVT_GROUP_DELETED]
        self.assertEqual(len(deleted), 1, deleted)


    def test_delete_behind_an_ownership_transfer_rechecks_the_owner(self):
        from groups.services import transfer_group_ownership

        heir = User.objects.create_user(username="h1638", password="p", email="h1638@example.com")
        GroupMembership.objects.create(group=self.group, user=heir, role=GroupMembership.Role.ADMIN)

        def transfer():
            with transaction.atomic():
                transfer_group_ownership(self.group_id, heir.pk, expected_owner_id=self.sender.pk)
                _checkpoint()
            return "transferred"

        transferred, deleted = self._race((_this_module(), "_checkpoint", False), transfer, self._delete)
        self.assertEqual(transferred, "transferred")
        self.assertEqual(deleted.status_code, 403, deleted.content[:300])
        self.assertEqual(Group.objects.get(pk=self.group_id).owner_id, heir.pk)


class SubgroupJoinVsParentDeleteTests(_GroupDeleteRaceBase):
    """The delete cascades to subgroups, so it must lock them too."""

    def make_link(self, **kwargs):
        # parent -> child: the invite is on the child, the delete on the parent.
        if self.target_group is self.group:
            self.target_group = Group.objects.create(name="C1638", owner=self.sender, parent=self.group)
            GroupMembership.objects.create(
                group=self.target_group, user=self.sender, role=GroupMembership.Role.ADMIN,
            )
        super().make_link(**kwargs)

    def test_parent_delete_behind_subgroup_join(self):
        joined, deleted = self._race(self.JOIN_HOLD, self._join, self._delete)
        self.assertEqual(joined.status_code, 201, joined.content[:300])
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self._assert_group_gone_cleanly()

    def test_subgroup_join_behind_parent_delete(self):
        deleted, joined = self._race(self.DELETE_HOLD, self._delete, self._join)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(joined.status_code, 404, joined.content[:300])
        self._assert_group_gone_cleanly()
        self._assert_no_member_added()


class RegistrationVsGroupDeleteTests(_GroupDeleteRaceBase):
    def setUp(self):
        super().setUp()
        _set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.addCleanup(_set_mode, SiteSetting.RegistrationMode.OPEN)

    def test_group_delete_behind_registration(self):
        registered, deleted = self._race(self.GRANT_HOLD, self._register, self._delete)
        self.assertIn(registered.status_code, (201, 204), registered.content[:300])
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self.assertTrue(User.objects.filter(email=REGISTERED).exists())
        self._assert_group_gone_cleanly()

    def test_registration_behind_group_delete(self):
        deleted, registered = self._race(self.DELETE_HOLD, self._delete, self._register)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(registered.status_code, 400, registered.content[:300])
        self.assertIn("invite_token", registered.json())
        self.assertFalse(User.objects.filter(email=REGISTERED).exists())
        self._assert_group_gone_cleanly()
        self._assert_no_member_added()


class ConfirmationGrantVsGroupDeleteTests(_GroupDeleteRaceBase):
    """The emailed-invite grant at address confirmation (#1704) vs a group delete."""

    def setUp(self):
        super().setUp()
        self.pending = User.objects.create_user(username="p1638", password="p", email=INVITED)
        self.address = EmailAddress.objects.create(
            user=self.pending, email=INVITED, verified=True, primary=True,
        )
        GroupInviteLink.objects.filter(pk=self.link.pk).update(reserved_for=self.pending)

    def make_link(self, **kwargs):
        super().make_link(recipient_email=INVITED, **kwargs)

    def _grant(self):
        return redeem_reserved_group_invites(
            User.objects.get(pk=self.pending.pk), EmailAddress.objects.get(pk=self.address.pk),
        )

    def test_group_delete_behind_grant(self):
        granted, deleted = self._race(self.GRANT_HOLD, self._grant, self._delete)
        self.assertEqual(granted, 1)
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self._assert_group_gone_cleanly()

    def test_grant_behind_group_delete(self):
        deleted, granted = self._race(self.DELETE_HOLD, self._delete, self._grant)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(granted, 0)
        self.assertFalse(GroupMembership.objects.filter(user=self.pending).exists())
        self._assert_group_gone_cleanly()
        self._assert_no_member_added()
