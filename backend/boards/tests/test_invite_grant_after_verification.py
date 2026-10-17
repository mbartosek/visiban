"""Group and board invite memberships start when the invited address is verified (#1704).

Under ``EMAIL_VERIFICATION=mandatory``, registering from an emailed (bound)
group or board invite with the invited address not yet verified reserves the
invite for the new account (``reserved_for``) without granting or consuming
it. Confirming the invited address grants the membership and consumes the
invite. ``optional`` / ``none``, provider-verified OAuth addresses and unbound
invites are granted at registration, as before.

Every scenario runs for both kinds through the ``_BoardKind`` / ``_GroupKind``
mixins. The concurrent cases need PostgreSQL (``SELECT ... FOR UPDATE``).
"""
import threading
from datetime import timedelta
from unittest import mock

from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.adapter import PENDING_INVITE_SESSION_KEY, SocialRegistrationAdapter
from accounts.models import SiteSetting, User
from boards.models import Board, BoardEvent, BoardInviteLink, BoardInviteRedemption, BoardMembership
from boards.tests.invite_fixtures import generate_invite
from groups.models import Group, GroupInviteLink, GroupMembership
from visiban.invite_recipient import grant_reserved_invites

PASSWORD = "Sup3rS3cr3t!xyz"
REGISTER_URL = "/api/v1/auth/registration/"
VERIFY_EMAIL_URL = "/api/v1/auth/registration/verify-email/"
INVITED = "Invitee.Person@Example.org"
MANDATORY = override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")


def set_mode(mode):
    row = SiteSetting.get()
    row.registration_mode = mode
    row.save()
    cache.clear()


class _BoardKind:
    model = BoardInviteLink

    def make_target(self):
        owner = User.objects.create_user(username="owner", password="p", email="owner@acme.test")
        self.sender = User.objects.create_user(
            username="sender", password="p", email="sender@acme.test", is_site_admin=True,
        )
        self.board = Board.objects.create(name="Launch Plan", owner=owner)
        BoardMembership.objects.create(board=self.board, user=self.sender, role=BoardMembership.Role.ADMIN)

    def make_invite(self, *, recipient=INVITED, expires_in=timedelta(days=7)):
        return generate_invite(
            BoardInviteLink,
            board=self.board, created_by=self.sender, role="member",
            expires_at=timezone.now() + expires_in, single_use=True, delivery="email",
            recipient_email=recipient,
        )

    def is_member(self, user):
        return BoardMembership.objects.filter(board=self.board, user=user).exists()

    def member_count(self):
        return BoardMembership.objects.filter(board=self.board, role="member").count()

    def revoke(self, link):
        BoardInviteLink.objects.filter(pk=link.pk).update(revoked_at=timezone.now())

    def join_url(self, raw):
        return f"/api/v1/boards/join/{raw}/"


class _GroupKind:
    model = GroupInviteLink

    def make_target(self):
        self.sender = User.objects.create_user(
            username="gadmin", password="p", email="gadmin@acme.test", is_site_admin=True,
        )
        self.group = Group.objects.create(name="Platform Team", owner=self.sender)
        GroupMembership.objects.create(group=self.group, user=self.sender, role=GroupMembership.Role.ADMIN)

    def make_invite(self, *, recipient=INVITED, expires_in=timedelta(days=7)):
        return generate_invite(
            GroupInviteLink,
            group=self.group, created_by=self.sender, role=GroupInviteLink.Role.MEMBER,
            expires_at=timezone.now() + expires_in, single_use=True,
            delivery=GroupInviteLink.Delivery.EMAIL, recipient_email=recipient,
        )

    def is_member(self, user):
        return GroupMembership.objects.filter(group=self.group, user=user).exists()

    def member_count(self):
        return GroupMembership.objects.filter(group=self.group, role="member").count()

    def revoke(self, link):
        GroupInviteLink.objects.filter(pk=link.pk).update(is_active=False)

    def join_url(self, raw):
        return f"/api/v1/groups/join/{raw}/"


def _confirm(address):
    return APIClient().post(VERIFY_EMAIL_URL, {"key": EmailConfirmationHMAC(address).key}, format="json")


class _RestRegistrationTests:
    """REST sign-up on an INVITE_ONLY site, then confirmation through the verify-email endpoint."""

    def setUp(self):
        cache.clear()
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.make_target()

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)
        cache.clear()

    def _register(self, email, raw):
        return APIClient().post(REGISTER_URL, {
            "email": email, "password1": PASSWORD, "password2": PASSWORD, "invite_token": raw,
        })

    def _registered(self):
        return User.objects.get(email__iexact=INVITED)

    def _invited_address(self, user):
        return EmailAddress.objects.get(user=user, email__iexact=INVITED)

    @MANDATORY
    def test_mandatory_reserves_at_registration_and_grants_on_confirmation(self):
        link, raw = self.make_invite()
        r = self._register(INVITED, raw)
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        # The response is the ordinary verify-email answer.
        self.assertEqual(set(r.json()), {"detail"})
        user = self._registered()
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertEqual(link.reserved_for_id, user.pk)

        with self.captureOnCommitCallbacks(execute=True):
            confirmed = _confirm(self._invited_address(user))
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK, confirmed.content)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @MANDATORY
    def test_repeat_confirmation_grants_once(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        address = self._invited_address(user)
        _confirm(address)
        # A second email_confirmed for the same address finds nothing reserved.
        address.refresh_from_db()
        grant_reserved_invites(sender=EmailAddress, request=None, email_address=address)
        self.assertEqual(self.member_count(), 1)

    @MANDATORY
    def test_reserved_invite_admits_no_second_registration(self):
        _link, raw = self.make_invite()
        self._register(INVITED, raw)
        r = self._register("second.person@example.org", raw)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
        self.assertIn("invite_token", r.json())
        self.assertFalse(User.objects.filter(email="second.person@example.org").exists())

    @MANDATORY
    def test_revoked_before_confirmation_grants_nothing(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        self.revoke(link)
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @MANDATORY
    def test_expired_before_confirmation_grants_nothing(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        self.model.objects.filter(pk=link.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @MANDATORY
    def test_confirming_a_different_address_grants_nothing(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        other = EmailAddress.objects.create(user=user, email="elsewhere@example.org", verified=False, primary=False)
        self.assertEqual(_confirm(other).status_code, status.HTTP_200_OK)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertEqual(link.reserved_for_id, user.pk)
        # The invited address still grants it when confirmed.
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertTrue(self.is_member(user))

    @MANDATORY
    def test_invite_joined_by_its_verified_recipient_first_is_not_granted_again(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        pending = self._registered()
        # The mailbox owner, already holding a verified account with the
        # invited address as a secondary, joins with the invite.
        existing = User.objects.create_user(username="existing", password="p", email="existing@example.org")
        EmailAddress.objects.create(user=existing, email=INVITED, verified=True, primary=False)
        client = APIClient()
        client.force_authenticate(existing)
        self.assertEqual(client.post(self.join_url(raw)).status_code, status.HTTP_201_CREATED)
        # The pending account cannot confirm the same address (verified on
        # another account), and is granted nothing either way.
        _confirm(self._invited_address(pending))
        grant_reserved_invites(sender=EmailAddress, request=None, email_address=self._invited_address(pending))
        self.assertFalse(self.is_member(pending))
        self.assertEqual(self.member_count(), 1)

    @MANDATORY
    def test_mandatory_unbound_invite_is_granted_at_registration(self):
        link, raw = self.make_invite(recipient=None)
        self.assertIn(self._register("anyone@example.org", raw).status_code, (201, 204))
        user = User.objects.get(email="anyone@example.org")
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    def _assert_granted_at_registration(self):
        link, raw = self.make_invite()
        r = self._register(INVITED, raw)
        self.assertIn(r.status_code, (201, 204), r.content)
        user = self._registered()
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_optional_grants_at_registration(self):
        self._assert_granted_at_registration()

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="none")
    def test_none_grants_at_registration(self):
        self._assert_granted_at_registration()


class BoardRestRegistrationTests(_BoardKind, _RestRegistrationTests, TestCase):

    @MANDATORY
    def test_grant_on_confirmation_records_and_broadcasts_member_added(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        with mock.patch("boards.broadcast.broadcast_board_event") as bcast:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                _confirm(self._invited_address(user))
            # Deferred to commit: nothing published inside the transaction.
            bcast.assert_not_called()
            for callback in callbacks:
                callback()
        self.assertEqual(
            [c.args[:2] for c in bcast.call_args_list], [(self.board.pk, "member.added")],
        )
        self.assertTrue(BoardEvent.objects.filter(board_id=self.board.pk, event="member.added").exists())
        redemption = BoardInviteRedemption.objects.get(invite=link, user=user)
        self.assertTrue(redemption.membership_created)
        self.assertEqual(redemption.via, BoardInviteRedemption.Via.REGISTRATION)
        link.refresh_from_db()
        self.assertEqual(link.use_count, 1)

    @MANDATORY
    def test_reserved_invite_preview_cannot_register(self):
        _link, raw = self.make_invite()
        self._register(INVITED, raw)
        r = APIClient().get(self.join_url(raw))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertFalse(r.json()["can_register"])


class GroupRestRegistrationTests(_GroupKind, _RestRegistrationTests, TestCase):

    @MANDATORY
    def test_grant_on_confirmation_broadcasts_member_added_on_commit(self):
        _link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        with mock.patch("groups.broadcast.broadcast_group_event") as bcast:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                _confirm(self._invited_address(user))
            bcast.assert_not_called()
            for callback in callbacks:
                callback()
        self.assertEqual([c.args[:2] for c in bcast.call_args_list], [(self.group.pk, "member.added")])


class _OAuthRegistrationTests:
    """OAuth sign-up on an INVITE_ONLY site (``SocialRegistrationAdapter.save_user``)."""

    def setUp(self):
        cache.clear()
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.make_target()
        self.adapter = SocialRegistrationAdapter()

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)
        cache.clear()

    def _save(self, raw, *, provider_verified):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.session = {PENDING_INVITE_SESSION_KEY: raw}

        def create(*args, **kwargs):
            # What allauth's setup_user_email leaves behind: the provider's
            # address, verified only when the provider says so.
            user = User.objects.create_user(username="oauth_user", password="p", email=INVITED.lower())
            EmailAddress.objects.create(user=user, email=INVITED.lower(), verified=provider_verified, primary=True)
            return user

        with mock.patch.object(SocialRegistrationAdapter.__bases__[0], "save_user", side_effect=create):
            return self.adapter.save_user(request, mock.MagicMock(), form=None)

    @MANDATORY
    def test_mandatory_unverified_provider_address_waits_for_confirmation(self):
        link, raw = self.make_invite()
        user = self._save(raw, provider_verified=False)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertEqual(link.reserved_for_id, user.pk)
        self.assertEqual(_confirm(EmailAddress.objects.get(user=user)).status_code, status.HTTP_200_OK)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @MANDATORY
    def test_mandatory_provider_verified_address_is_granted_at_signup(self):
        link, raw = self.make_invite()
        user = self._save(raw, provider_verified=True)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_optional_unverified_provider_address_is_granted_at_signup(self):
        link, raw = self.make_invite()
        user = self._save(raw, provider_verified=False)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.reserved_for_id)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="none")
    def test_none_unverified_provider_address_is_granted_at_signup(self):
        link, raw = self.make_invite()
        user = self._save(raw, provider_verified=False)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.reserved_for_id)


class BoardOAuthRegistrationTests(_BoardKind, _OAuthRegistrationTests, TestCase):
    pass


class GroupOAuthRegistrationTests(_GroupKind, _OAuthRegistrationTests, TestCase):
    pass


class SiteInviteUnchangedTests(TestCase):
    """A site invite admits the account itself; it is still consumed at registration."""

    def setUp(self):
        cache.clear()
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)
        cache.clear()

    @MANDATORY
    def test_mandatory_site_invite_is_consumed_at_registration(self):
        from accounts.models import InviteLink

        admin = User.objects.create_user(username="siteadm", password="p", is_site_admin=True)
        link, raw = InviteLink.generate(
            created_by=admin, single_use=True, delivery=InviteLink.Delivery.EMAIL, recipient_email=INVITED,
        )
        r =APIClient().post(REGISTER_URL, {
            "email": INVITED, "password1": PASSWORD, "password2": PASSWORD, "invite_token": raw,
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)


class _ConcurrentConfirmationTests:
    """Two simultaneous grants for one reserved invite yield exactly one membership."""

    def setUp(self):
        if connection.vendor == "sqlite":
            self.skipTest("SQLite does not support SELECT FOR UPDATE; this test requires PostgreSQL.")
        cache.clear()
        self.make_target()
        self.link, self.raw = self.make_invite()
        self.pending = User.objects.create_user(username="pending", password="p", email=INVITED.lower())
        self.address = EmailAddress.objects.create(
            user=self.pending, email=INVITED.lower(), verified=True, primary=True,
        )
        self.model.objects.filter(pk=self.link.pk).update(reserved_for=self.pending)

    def tearDown(self):
        cache.clear()

    def _run_concurrently(self, *targets):
        errors = []
        barrier = threading.Barrier(len(targets))

        def run(target):
            from django.db import connections as _conns

            try:
                barrier.wait()
                target()
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)
            finally:
                _conns.close_all()

        threads = [threading.Thread(target=run, args=(t,)) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def _confirm_in_thread(self):
        address = EmailAddress.objects.get(pk=self.address.pk)
        grant_reserved_invites(sender=EmailAddress, request=None, email_address=address)

    def test_two_concurrent_confirmations_grant_once(self):
        with mock.patch("boards.broadcast.broadcast_board_event"), \
                mock.patch("groups.broadcast.broadcast_group_event"):
            self._run_concurrently(self._confirm_in_thread, self._confirm_in_thread)
        self.assertTrue(self.is_member(self.pending))
        self.assertEqual(self.member_count(), 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.used_at)
        self.assertIsNone(self.link.reserved_for_id)

    def test_confirmation_racing_a_join_grants_once(self):
        existing = User.objects.create_user(username="existing", password="p", email="existing@example.org")
        EmailAddress.objects.create(user=existing, email=INVITED, verified=True, primary=False)

        def join():
            client = APIClient()
            client.force_authenticate(User.objects.get(pk=existing.pk))
            client.post(self.join_url(self.raw))

        with mock.patch("boards.broadcast.broadcast_board_event"), \
                mock.patch("groups.broadcast.broadcast_group_event"):
            self._run_concurrently(self._confirm_in_thread, join)
        # Exactly one of the two accounts holds the invite's membership.
        self.assertEqual(self.member_count(), 1)
        self.assertNotEqual(self.is_member(self.pending), self.is_member(existing))
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.used_at)


class BoardConcurrentConfirmationTests(_BoardKind, _ConcurrentConfirmationTests, TransactionTestCase):

    def test_two_concurrent_confirmations_write_one_redemption(self):
        with mock.patch("boards.broadcast.broadcast_board_event"):
            self._run_concurrently(self._confirm_in_thread, self._confirm_in_thread)
        self.assertEqual(BoardInviteRedemption.objects.filter(invite=self.link).count(), 1)
        self.link.refresh_from_db()
        self.assertEqual(self.link.use_count, 1)


class GroupConcurrentConfirmationTests(_GroupKind, _ConcurrentConfirmationTests, TransactionTestCase):
    pass
