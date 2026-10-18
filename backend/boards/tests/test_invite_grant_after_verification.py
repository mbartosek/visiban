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
import importlib
import logging
import threading
from datetime import timedelta
from unittest import mock

from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from allauth.core.exceptions import ImmediateHttpResponse
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
from boards.tests.lock_wait import assert_blocked_on_lock
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
    grant_target = "boards.invites.redeem"

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
    grant_target = "groups.invite_registration._grant_membership"

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
    def test_reserved_invite_admits_no_second_rest_registration(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        first = self._registered()
        # Another address does not match the binding; the invited address is
        # already taken by the first account. Neither creates an account.
        for email in ("second.person@example.org", INVITED.upper()):
            with self.subTest(email=email):
                r = self._register(email, raw)
                self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
        self.assertEqual(User.objects.filter(email__iexact=INVITED).count(), 1)
        self.assertFalse(User.objects.filter(email="second.person@example.org").exists())
        link.refresh_from_db()
        self.assertEqual(link.reserved_for_id, first.pk)
        self.assertIsNone(link.used_at)

    @MANDATORY
    def test_role_outside_the_membership_roles_is_released_not_granted(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        self.model.objects.filter(pk=link.pk).update(role="owner")
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @MANDATORY
    def test_failing_grant_does_not_break_confirmation(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        with mock.patch(self.grant_target, side_effect=RuntimeError("boom")), \
                self.assertLogs("", level="ERROR") as logs:
            r = _confirm(self._invited_address(user))
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertTrue(self._invited_address(user).verified)
        self.assertFalse(self.is_member(user))
        # Rolled back: still reserved, still unused, so the join page still works.
        link.refresh_from_db()
        self.assertEqual(link.reserved_for_id, user.pk)
        self.assertIsNone(link.used_at)
        output = "\n".join(logs.output)
        self.assertIn(f"user_id={user.pk}", output)
        self.assertNotIn(INVITED.lower(), output.lower())
        self.assertNotIn(raw, output)

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

    @MANDATORY
    def test_inactive_account_is_not_granted_and_keeps_the_reservation(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        address = self._invited_address(user)
        EmailAddress.objects.filter(pk=address.pk).update(verified=True)
        User.objects.filter(pk=user.pk).update(is_active=False)
        address.refresh_from_db()
        grant_reserved_invites(sender=EmailAddress, request=None, email_address=address)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertEqual(link.reserved_for_id, user.pk)
        self.assertIsNone(link.used_at)

    @MANDATORY
    def test_deleting_the_reserving_account_frees_the_invite(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        self._registered().delete()
        link.refresh_from_db()
        self.assertIsNone(link.reserved_for_id)
        # The invited address can register from it again.
        self.assertEqual(self._register(INVITED, raw).status_code, status.HTTP_201_CREATED)
        link.refresh_from_db()
        self.assertEqual(link.reserved_for_id, self._registered().pk)

    def _assert_released_at_confirmation(self, link, user):
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertFalse(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @MANDATORY
    def test_sender_losing_site_admin_before_confirmation_releases(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        User.objects.filter(pk=self.sender.pk).update(is_site_admin=False)
        self._assert_released_at_confirmation(link, self._registered())

    @MANDATORY
    def test_email_invites_turned_off_before_confirmation_releases(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        with override_settings(INVITE_EMAIL_ENABLED=False):
            self._assert_released_at_confirmation(link, self._registered())

    @MANDATORY
    def test_closed_mode_still_grants_a_reservation_at_confirmation(self):
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        set_mode(SiteSetting.RegistrationMode.CLOSED)
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertTrue(self.is_member(user))

    @MANDATORY
    def test_allauth_admin_mark_verified_grants(self):
        from allauth.account.admin import EmailAddressAdmin
        from django.contrib.admin.sites import site

        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        address = self._invited_address(user)
        admin = EmailAddressAdmin(EmailAddress, site)
        with mock.patch.object(EmailAddressAdmin, "message_user"):
            admin.make_verified(RequestFactory().post("/"), EmailAddress.objects.filter(pk=address.pk))
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)

    @MANDATORY
    def test_failing_email_change_receiver_does_not_strand_the_reservation(self):
        # The grant receiver runs before the email-change receiver, so an error
        # there (which fails the request) leaves the grant already made.
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        address = self._invited_address(user)
        User.objects.filter(pk=user.pk).update(pending_email_address=address)
        with mock.patch("accounts.email_change._set_pending_pointer", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            _confirm(address)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)

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
    def test_reserved_invite_preview_still_offers_registration(self):
        # A sign-up that does not have to wait for verification may still
        # take a reserved invite over, so the preview keeps offering sign-up.
        _link, raw = self.make_invite()
        self._register(INVITED, raw)
        r = APIClient().get(self.join_url(raw))
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.json()["can_register"])

    @MANDATORY
    def test_failure_on_one_invite_leaves_the_others_granted(self):
        # A board invite and a group invite reserved for the same account; the
        # board grant fails, the group grant still happens.
        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        group = Group.objects.create(name="Platform Team", owner=self.sender)
        GroupMembership.objects.create(group=group, user=self.sender, role=GroupMembership.Role.ADMIN)
        glink, _graw = GroupInviteLink.generate(
            group=group, created_by=self.sender, role=GroupInviteLink.Role.MEMBER, single_use=True,
            delivery=GroupInviteLink.Delivery.EMAIL, recipient_email=INVITED,
            expires_at=timezone.now() + timedelta(days=7),
        )
        GroupInviteLink.objects.filter(pk=glink.pk).update(reserved_for=user)
        second_board = Board.objects.create(name="Second", owner=self.sender)
        link2, _raw2 = BoardInviteLink.generate(
            board=second_board, created_by=self.sender, role="viewer", single_use=True, delivery="email",
            recipient_email=INVITED, expires_at=timezone.now() + timedelta(days=7),
        )
        BoardInviteLink.objects.filter(pk=link2.pk).update(reserved_for=user)
        from boards import invites as board_invites

        real_redeem = board_invites.redeem
        calls = []

        def flaky(link_obj, *args, **kwargs):
            calls.append(link_obj.pk)
            if link_obj.pk == link.pk:
                raise RuntimeError("boom")
            return real_redeem(link_obj, *args, **kwargs)

        with mock.patch("boards.invites.redeem", side_effect=flaky), self.assertLogs("", level="ERROR"):
            r = _confirm(self._invited_address(user))
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertFalse(self.is_member(user))
        self.assertTrue(BoardMembership.objects.filter(board=second_board, user=user).exists())
        self.assertTrue(GroupMembership.objects.filter(group=group, user=user).exists())
        link.refresh_from_db()
        self.assertEqual(link.reserved_for_id, user.pk)


class GroupRestRegistrationTests(_GroupKind, _RestRegistrationTests, TestCase):

    @MANDATORY
    def test_deleted_group_grants_nothing_and_confirmation_succeeds(self):
        _link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        Group.objects.filter(pk=self.group.pk).delete()
        self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertFalse(GroupMembership.objects.filter(user=user).exists())

    @MANDATORY
    def test_group_deleted_between_listing_and_lock_is_skipped(self):
        from groups import invite_registration

        link, raw = self.make_invite()
        self._register(INVITED, raw)
        user = self._registered()
        real = invite_registration._grant_reserved_group_invite

        def delete_first(link_id, group_id, *args):
            Group.objects.filter(pk=group_id).delete()
            return real(link_id, group_id, *args)

        with mock.patch.object(invite_registration, "_grant_reserved_group_invite", side_effect=delete_first):
            self.assertEqual(_confirm(self._invited_address(user)).status_code, status.HTTP_200_OK)
        self.assertFalse(GroupMembership.objects.filter(user=user).exists())

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

    def _save(self, raw, *, provider_verified, username="oauth_user"):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.session = {PENDING_INVITE_SESSION_KEY: raw}

        def create(*args, **kwargs):
            # What allauth's setup_user_email leaves behind: the provider's
            # address, verified only when the provider says so.
            user = User.objects.create_user(username=username, password="p", email=INVITED.lower())
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

    def _reserve_for_another_account(self, link):
        holder = User.objects.create_user(username="holder", password="p", email=INVITED.lower())
        self.model.objects.filter(pk=link.pk).update(reserved_for=holder)
        return holder

    @MANDATORY
    def test_second_awaiting_signup_cannot_take_a_reserved_invite(self):
        link, raw = self.make_invite()
        holder = self._reserve_for_another_account(link)
        with self.assertRaises(ImmediateHttpResponse) as ctx:
            self._save(raw, provider_verified=False)
        self.assertIn("auth_error=invite_invalid", ctx.exception.response.url)
        self.assertFalse(User.objects.filter(username="oauth_user").exists())
        link.refresh_from_db()
        self.assertEqual(link.reserved_for_id, holder.pk)
        self.assertIsNone(link.used_at)

    @MANDATORY
    def test_provider_verified_signup_takes_over_a_reservation(self):
        link, raw = self.make_invite()
        holder = self._reserve_for_another_account(link)
        user = self._save(raw, provider_verified=True)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)
        # The earlier reserving account is granted nothing later.
        holder_address = EmailAddress.objects.create(
            user=holder, email="holder@example.org", verified=True, primary=True,
        )
        grant_reserved_invites(sender=EmailAddress, request=None, email_address=holder_address)
        self.assertFalse(self.is_member(holder))
        self.assertEqual(self.member_count(), 1)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_optional_mode_signup_takes_over_a_reservation(self):
        link, raw = self.make_invite()
        holder = self._reserve_for_another_account(link)
        user = self._save(raw, provider_verified=False)
        self.assertTrue(self.is_member(user))
        self.assertFalse(self.is_member(holder))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertIsNone(link.reserved_for_id)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="none")
    def test_none_mode_signup_takes_over_a_reservation(self):
        link, raw = self.make_invite()
        self._reserve_for_another_account(link)
        user = self._save(raw, provider_verified=False)
        self.assertTrue(self.is_member(user))
        link.refresh_from_db()
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


HOLD_TIMEOUT = 10


class _ErrorRecords(logging.Handler):
    """Collects ERROR records from every thread (handlers are process-wide)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class _ConcurrentConfirmationTests:
    """Concurrent grants at confirmation, interleaved deterministically (PostgreSQL).

    Each test pauses the first caller at a fixed point inside its transaction,
    proves the second one waits on a database lock there
    (``assert_blocked_on_lock``), then releases the first. Grants are counted
    from the functions' return values, and any ERROR record (a swallowed
    failure) fails the test, so a duplicate grant cannot hide behind the
    per-invite exception handler or a unique constraint.
    """

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
        self.errors = _ErrorRecords()
        logging.getLogger().addHandler(self.errors)

    def tearDown(self):
        logging.getLogger().removeHandler(self.errors)
        cache.clear()

    def _hold_first_caller_after(self, attr):
        """Patch ``attr`` of the kind's module so its first caller pauses right after it returns."""
        module = importlib.import_module(self.grant_module)
        real = getattr(module, attr)
        reached, release = threading.Event(), threading.Event()
        state = {"first": True}
        guard = threading.Lock()

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            with guard:
                first, state["first"] = state["first"], False
            if first:
                reached.set()
                release.wait(HOLD_TIMEOUT)
            return result

        return mock.patch.object(module, attr, side_effect=wrapper), reached, release

    def _start(self, target, results, name):
        def run():
            from django.db import connections as _conns

            try:
                results.append((name, target()))
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertions
                results.append((name, exc))
            finally:
                _conns.close_all()

        thread = threading.Thread(target=run, name=name)
        thread.start()
        return thread

    def _grant(self):
        address = EmailAddress.objects.get(pk=self.address.pk)
        return self.redeem_reserved(User.objects.get(pk=self.pending.pk), address)

    def _no_errors(self):
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    def test_second_confirmation_waits_on_the_container_row_lock(self):
        patcher, reached, release = self._hold_first_caller_after(self.row_lock_attr)
        results = []
        with patcher, mock.patch("boards.broadcast.broadcast_board_event"), \
                mock.patch("groups.broadcast.broadcast_group_event"):
            first = self._start(self._grant, results, "first")
            self.assertTrue(reached.wait(HOLD_TIMEOUT))
            second = self._start(self._grant, results, "second")
            try:
                assert_blocked_on_lock(self, second, "second confirmation must wait on the row lock")
            finally:
                release.set()
                first.join()
                second.join()
        self.assertEqual(sorted(name for name, _ in results), ["first", "second"])
        self.assertTrue(all(isinstance(v, int) for _, v in results), results)
        self.assertEqual(sum(v for _, v in results), 1, results)
        self.assertEqual(dict(results)["first"], 1, results)
        self.assertEqual(self.member_count(), 1)
        self.assertTrue(self.is_member(self.pending))
        self._no_errors()

    def test_invite_row_lock_holds_off_another_writer(self):
        # Paused after the invite row lock: a writer that takes only the invite
        # row (``other_invite_writer``) must wait for the grant to commit.
        patcher, reached, release = self._hold_first_caller_after("confirmed_address_admits")
        results = []
        with patcher, mock.patch("boards.broadcast.broadcast_board_event"), \
                mock.patch("groups.broadcast.broadcast_group_event"):
            first = self._start(self._grant, results, "grant")
            self.assertTrue(reached.wait(HOLD_TIMEOUT))
            second = self._start(self.other_invite_writer, results, "writer")
            try:
                assert_blocked_on_lock(self, second, "a writer of the invite row must wait on the grant's row lock")
            finally:
                release.set()
                first.join()
                second.join()
        outcome = dict(results)
        self.assertEqual(outcome["grant"], 1, results)
        self.assertTrue(self.is_member(self.pending))
        self.assertEqual(self.member_count(), 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.used_at)
        self._no_errors()

    def test_claim_is_conditional_even_after_a_usable_read(self):
        # The locked read is made to report a usable invite that is in fact
        # used; the conditional claim (used_at IS NULL, checked by row count)
        # must still refuse it.
        self.model.objects.filter(pk=self.link.pk).update(used_at=timezone.now())
        with mock.patch(f"{self.grant_module}.{self.refusal_attr}", return_value=None):
            granted = self._grant()
        self.assertEqual(granted, 0)
        self.assertFalse(self.is_member(self.pending))
        self._no_errors()


class BoardConcurrentConfirmationTests(_BoardKind, _ConcurrentConfirmationTests, TransactionTestCase):
    grant_module = "boards.invites"
    row_lock_attr = "lock_invite_board"
    refusal_attr = "board_link_registration_refusal"

    @staticmethod
    def redeem_reserved(user, address):
        from boards.invites import redeem_reserved_board_invites

        return redeem_reserved_board_invites(user, address)

    def other_invite_writer(self):
        # An invite revoke takes only the invite row, no board lock.
        return BoardInviteLink.objects.filter(pk=self.link.pk).update(revoked_at=timezone.now())

    def test_join_racing_a_confirmation_grants_once(self):
        existing = User.objects.create_user(username="existing", password="p", email="existing@example.org")
        EmailAddress.objects.create(user=existing, email=INVITED, verified=True, primary=False)

        def join():
            client = APIClient()
            client.force_authenticate(User.objects.get(pk=existing.pk))
            return client.post(self.join_url(self.raw)).status_code

        patcher, reached, release = self._hold_first_caller_after("confirmed_address_admits")
        results = []
        with patcher, mock.patch("boards.broadcast.broadcast_board_event"):
            first = self._start(self._grant, results, "grant")
            self.assertTrue(reached.wait(HOLD_TIMEOUT))
            second = self._start(join, results, "join")
            try:
                assert_blocked_on_lock(self, second, "the join must wait on the grant's locks")
            finally:
                release.set()
                first.join()
                second.join()
        outcome = dict(results)
        self.assertEqual(outcome["grant"], 1, results)
        self.assertEqual(outcome["join"], 410, results)
        self.assertEqual(BoardInviteRedemption.objects.filter(invite=self.link).count(), 1)
        self.link.refresh_from_db()
        self.assertEqual(self.link.use_count, 1)
        self._no_errors()


class GroupConcurrentConfirmationTests(_GroupKind, _ConcurrentConfirmationTests, TransactionTestCase):
    grant_module = "groups.invite_registration"
    row_lock_attr = "_lock_invite_group"
    refusal_attr = "group_link_registration_refusal"

    @staticmethod
    def redeem_reserved(user, address):
        from groups.invite_registration import redeem_reserved_group_invites

        return redeem_reserved_group_invites(user, address)

    def other_invite_writer(self):
        # The group join path locks only the invite row.
        existing = User.objects.create_user(username="existing", password="p", email="existing@example.org")
        EmailAddress.objects.create(user=existing, email=INVITED, verified=True, primary=False)
        client = APIClient()
        client.force_authenticate(existing)
        return client.post(self.join_url(self.raw)).status_code


class _ConcurrentSignupTests:
    """Simultaneous OAuth sign-ups on one invite (PostgreSQL)."""

    def setUp(self):
        if connection.vendor == "sqlite":
            self.skipTest("SQLite does not support SELECT FOR UPDATE; this test requires PostgreSQL.")
        cache.clear()
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.make_target()
        self.link, self.raw = self.make_invite()

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)
        cache.clear()

    def _race_oauth_signups(self, usernames):
        results = []
        barrier = threading.Barrier(len(usernames))
        local = threading.local()

        def create(*args, **kwargs):
            # One patch for both threads (patching a class attribute per thread
            # would race); each thread names its own account.
            return User.objects.create_user(username=local.username, password="p", email=INVITED.lower())

        def signup(username):
            from django.db import connections as _conns

            try:
                local.username = username
                request = RequestFactory().get("/accounts/google/login/callback/")
                request.session = {PENDING_INVITE_SESSION_KEY: self.raw}
                barrier.wait()
                try:
                    SocialRegistrationAdapter().save_user(request, mock.MagicMock(), form=None)
                    results.append("ok")
                except ImmediateHttpResponse:
                    results.append("refused")
                except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                    results.append(repr(exc))
            finally:
                _conns.close_all()

        with mock.patch("boards.broadcast.broadcast_board_event"), \
                mock.patch("groups.broadcast.broadcast_group_event"), \
                mock.patch.object(SocialRegistrationAdapter.__bases__[0], "save_user", side_effect=create):
            threads = [threading.Thread(target=signup, args=(name,)) for name in usernames]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        return results

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_two_concurrent_takeovers_grant_once(self):
        holder = User.objects.create_user(username="holder", password="p", email=INVITED.lower())
        self.model.objects.filter(pk=self.link.pk).update(reserved_for=holder)
        results = self._race_oauth_signups(["taker_a", "taker_b"])
        self.assertEqual(sorted(results), ["ok", "refused"], results)
        self.assertEqual(self.member_count(), 1)
        self.assertFalse(self.is_member(holder))
        self.assertEqual(User.objects.filter(username__in=["taker_a", "taker_b"]).count(), 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.used_at)
        self.assertIsNone(self.link.reserved_for_id)

    @MANDATORY
    def test_two_concurrent_reservations_reserve_once(self):
        results = self._race_oauth_signups(["waiter_a", "waiter_b"])
        self.assertEqual(sorted(results), ["ok", "refused"], results)
        survivors = list(User.objects.filter(username__in=["waiter_a", "waiter_b"]))
        self.assertEqual(len(survivors), 1)
        self.link.refresh_from_db()
        self.assertEqual(self.link.reserved_for_id, survivors[0].pk)
        self.assertIsNone(self.link.used_at)
        self.assertEqual(self.member_count(), 0)


class BoardConcurrentSignupTests(_BoardKind, _ConcurrentSignupTests, TransactionTestCase):
    pass


class GroupConcurrentSignupTests(_GroupKind, _ConcurrentSignupTests, TransactionTestCase):
    pass


class GroupBroadcastFailureTests(_GroupKind, TransactionTestCase):
    """A failing group broadcast after commit does not fail the confirmation or undo the grant."""

    def setUp(self):
        cache.clear()
        self.make_target()
        self.link, _raw = self.make_invite()
        self.user = User.objects.create_user(username="pending", password="p", email=INVITED.lower())
        self.address = EmailAddress.objects.create(user=self.user, email=INVITED.lower(), verified=False, primary=True)
        GroupInviteLink.objects.filter(pk=self.link.pk).update(reserved_for=self.user)

    def tearDown(self):
        cache.clear()

    @MANDATORY
    def test_broadcast_failure_is_not_fatal(self):
        with mock.patch("groups.broadcast.broadcast_group_event", side_effect=RuntimeError("down")) as bcast:
            r = _confirm(self.address)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertTrue(bcast.called)
        self.assertTrue(self.is_member(self.user))
