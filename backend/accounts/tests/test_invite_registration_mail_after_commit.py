"""Invite-only registration sends its signup mail after the commit (#1639).

``InviteRegisterView.create`` creates the account and redeems the invite in one
transaction that holds the board row (``FOR NO KEY UPDATE``) and then the
invite row for a board invite (#1631), or the invite row for a site or group
invite. allauth's ``complete_signup`` sent the confirmation mail inside that
transaction, so an SMTP round trip (unbounded with ``EMAIL_TIMEOUT`` unset)
held every writer of that board. The mail is now queued with
``transaction.on_commit``.

* ``MailNotSentUnderLockTests`` (PostgreSQL): a mail backend probes, from a
  second connection, whether the board and invite rows are still locked at the
  moment the mail is sent. Before the fix both probes found the rows locked.
* ``InviteRegistrationMailTests`` (every backend): for board, site and group
  invites the mail is queued until the commit and then goes out once, a
  rolled-back registration sends none, and a send failure after the commit
  leaves the account in place instead of answering 500.
"""

import threading
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.mail.backends.locmem import EmailBackend as LocmemBackend
from django.db import DatabaseError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.invite_utils import InviteTokenError
from accounts.models import InviteLink, SiteSetting, User
from boards.models import Board, BoardInviteLink, BoardMembership
from boards.tests.invite_fixtures import generate_invite
from groups.models import Group, GroupInviteLink, GroupMembership

PASSWORD = "Sup3rS3cr3t!xyz"
REGISTER_URL = "/api/v1/auth/registration/"

# What the probing backend saw, per sent message: {"board": bool, "invite": bool}
# where True means the row was still locked by the registering transaction.
PROBES: list[dict] = []
PROBE_TARGET: dict = {}


def _row_is_locked(model, pk) -> bool:
    """True when another transaction holds a lock on ``model`` row ``pk``.

    Runs in its own thread, so on its own connection: ``FOR UPDATE NOWAIT``
    conflicts with the registration's ``FOR NO KEY UPDATE`` (board) and
    ``FOR UPDATE`` (invite) and fails at once instead of waiting.
    """
    result = {}

    def probe():
        try:
            with transaction.atomic():
                list(model.objects.select_for_update(nowait=True).filter(pk=pk).values_list("pk", flat=True))
            result["locked"] = False
        except DatabaseError:
            result["locked"] = True
        finally:
            connections.close_all()  # threaded-test convention (backend CLAUDE.md)

    t = threading.Thread(target=probe)
    t.start()
    t.join()  # NOWAIT never blocks; this only collects the answer
    return result["locked"]


class LockProbeBackend(LocmemBackend):
    """locmem backend that records whether the invite's rows are locked at send time."""

    def send_messages(self, messages):
        PROBES.append(
            {
                "board": _row_is_locked(Board, PROBE_TARGET["board"]),
                "invite": _row_is_locked(BoardInviteLink, PROBE_TARGET["invite"]),
            }
        )
        return super().send_messages(messages)


def _set_mode(mode):
    row = SiteSetting.get()
    row.registration_mode = mode
    row.save()
    cache.clear()  # drop the cached registration mode


def _register(raw, email, client=None):
    return (client or APIClient()).post(
        REGISTER_URL,
        {"email": email, "password1": PASSWORD, "password2": PASSWORD, "invite_token": raw},
        format="json",
    )


class _BoardInviteMixin:
    def _make_board_invite(self):
        self.owner = User.objects.create_user(username="o1639", password="p")
        self.sender = User.objects.create_user(username="s1639", password="p", is_site_admin=True)
        self.board = Board.objects.create(name="B1639", owner=self.owner)
        BoardMembership.objects.create(board=self.board, user=self.owner, role="admin")
        BoardMembership.objects.create(board=self.board, user=self.sender, role="admin")
        return generate_invite(
            BoardInviteLink,
            board=self.board,
            created_by=self.sender,
            role="member",
            single_use=True,
            delivery="email",
            expires_at=timezone.now() + timedelta(days=7),
        )


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
@override_settings(
    EMAIL_BACKEND="accounts.tests.test_invite_registration_mail_after_commit.LockProbeBackend"
)
class MailNotSentUnderLockTests(_BoardInviteMixin, TransactionTestCase):
    def setUp(self):
        cache.clear()
        PROBES.clear()
        self.link, self.raw = self._make_board_invite()
        PROBE_TARGET.update(board=self.board.pk, invite=self.link.pk)
        _set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cache.clear)
        self.addCleanup(_set_mode, SiteSetting.RegistrationMode.OPEN)

    def test_confirmation_mail_is_sent_with_no_board_or_invite_lock_held(self):
        resp = _register(self.raw, "new1639@example.com")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertTrue(BoardMembership.objects.filter(board=self.board, user__email="new1639@example.com").exists())
        self.assertEqual(len(PROBES), 1, "expected exactly one signup confirmation mail")
        self.assertEqual(PROBES[0], {"board": False, "invite": False})


class InviteRegistrationMailTests(_BoardInviteMixin, TestCase):
    def setUp(self):
        cache.clear()
        _set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cache.clear)

    def test_board_invite_registration_sends_confirmation_after_commit(self):
        _link, raw = self._make_board_invite()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = _register(raw, "ok1639@example.com")
            self.assertEqual(resp.status_code, 201, resp.content[:300])
            # Queued, not sent, while the registering transaction is open.
            self.assertEqual(mail.outbox, [])
        for callback in callbacks:
            callback()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ok1639@example.com"])

    def test_site_invite_registration_sends_confirmation_after_commit(self):
        creator = User.objects.create_user(username="c1639", password="p", is_site_admin=True)
        _link, raw = InviteLink.generate(created_by=creator)
        self._assert_sent_only_on_commit(raw, "site1639@example.com")

    def test_group_invite_registration_sends_confirmation_after_commit(self):
        sender = User.objects.create_user(username="g1639", password="p", is_site_admin=True)
        group = Group.objects.create(name="G1639", owner=sender)
        GroupMembership.objects.create(group=group, user=sender, role=GroupMembership.Role.ADMIN)
        _link, raw = generate_invite(
            GroupInviteLink,
            group=group,
            created_by=sender,
            role=GroupInviteLink.Role.VIEWER,
            single_use=True,
            delivery=GroupInviteLink.Delivery.EMAIL,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self._assert_sent_only_on_commit(raw, "group1639@example.com")
        self.assertTrue(GroupMembership.objects.filter(group=group, user__email="group1639@example.com").exists())

    def _assert_sent_only_on_commit(self, raw, email):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = _register(raw, email)
            self.assertEqual(resp.status_code, 201, resp.content[:300])
            # Queued, not sent, while the registering transaction is open.
            self.assertEqual(mail.outbox, [])
        for callback in callbacks:
            callback()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [email])

    def test_rolled_back_registration_sends_no_mail(self):
        # A redemption refused after the account was created rolls the account
        # back; the mail for it must not leave (before #1639 it already had).
        _link, raw = self._make_board_invite()
        # One client, already holding a session from a prior signup, as in
        # boards/tests/test_board_invite_registration.py's 409 tests: a fresh
        # client's session row is created inside the rolled-back transaction,
        # and the session middleware then answers 400 instead of the view's 409.
        client = APIClient()
        creator = User.objects.create_user(username="c1639r", password="p", is_site_admin=True)
        _site, site_raw = InviteLink.generate(created_by=creator)
        with self.captureOnCommitCallbacks(execute=True):
            first = _register(site_raw, "first1639@example.com", client=client)
        self.assertEqual(first.status_code, 201, first.content[:300])
        mail.outbox.clear()
        with patch(
            "boards.invites.redeem_board_registration_token",
            side_effect=InviteTokenError("invite_invalid", "This invite link is invalid."),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                resp = _register(raw, "gone1639@example.com", client=client)
        self.assertEqual(resp.status_code, 409, resp.content[:300])
        self.assertFalse(User.objects.filter(email="gone1639@example.com").exists())
        self.assertEqual(mail.outbox, [])

    def test_send_failure_after_commit_keeps_the_account(self):
        _link, raw = self._make_board_invite()
        with patch(
            "django.core.mail.backends.locmem.EmailBackend.send_messages",
            side_effect=OSError("smtp down"),
        ), self.assertLogs("accounts.adapter", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                resp = _register(raw, "smtp1639@example.com")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertTrue(User.objects.filter(email="smtp1639@example.com").exists())
        output = "\n".join(logs.output)
        self.assertIn("OSError", output)
        self.assertNotIn("smtp down", output)
        self.assertNotIn("smtp1639@example.com", output)

    def test_open_mode_registration_still_sends_inline(self):
        # The deferral is scoped to invite-only registration; open mode is unchanged.
        _set_mode(SiteSetting.RegistrationMode.OPEN)
        resp = _register("", "open1639@example.com")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertEqual(len(mail.outbox), 1)
