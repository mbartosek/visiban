"""Side effects that follow a password being set through the web interface, the API, the Django admin or the operator command line (#1551).

A password can be set through several routes: the SPA's two REST change
endpoints, the REST reset-confirm endpoint, allauth's HTML change, set and
reset-by-key forms, the Django admin password form and the ``changepassword``
management command. Each route calls the
helpers here in the same transaction as its password save, so the follow-up
work is the same on each of them and commits or rolls back with the password.
Registry Rule 3 in ``docs/development/security-invariants.md`` lists the
callers.

The helpers are deliberately not attached to ``User.set_password``: that method
also runs on throwaway, unsaved instances (``accounts.backends.EmailBackend``
hashes a password on one to equalize login timing), and those must not touch
any stored row.
"""

from __future__ import annotations

import time

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.authentication import SessionAuthentication
from rest_framework.request import Request


def lock_user_row(user):
    """Lock *user*'s row and return the freshly read row.

    Credential issuance (``PersonalAccessToken`` creation,
    ``reissue_auth_token``, and the DRF ``Token`` issued at REST login in
    ``accounts.views.ThrottledLoginView.login``) and credential
    revocation (``finalize_password_change``, the admin-set variant and the
    operator-reset variant) take this lock first, so they are serialized per user. Must be called inside
    ``transaction.atomic()``. SQLite has no row locks, so there it is a plain
    read.
    """
    return get_user_model().objects.select_for_update().get(pk=user.pk)


def finalize_password_change(user, *, first_password=False):
    """Side effects every successful user-initiated password change must have.

    Shared by the routes listed in the module docstring through which a user
    sets their own password. Every caller runs it inside the same transaction as the
    password save, so the new password and these writes commit together. Its
    own ``atomic()`` block keeps its two writes together in any case.

    - Clears ``must_change_password``: choosing a new password is exactly what
      the forced-change flag asks for, whichever of those routes the user reached.
    - Revokes every Personal Access Token: ``PersonalAccessToken`` documents
      "all tokens are deleted when the password changes", and users are told
      rotating the password is how to cut off a leaked token (#406, #1110).
    - Revokes the DRF auth ``Token`` (#1553, #1562): a password change revokes
      every credential issued under the old password. The changer is not
      logged out: the SPA authenticates with its session cookie, which the
      REST views and allauth keep alive with ``update_session_auth_hash``, and
      a REST caller that authenticated with the ``Token`` gets a new one from
      ``reissue_auth_token`` in the same transaction.

    ``must_change_username`` is left alone: a password change does not answer
    a username request. Idempotent, so a second call is harmless.

    *first_password* is true when the account had no usable password before
    this write; the caller reads that from the row it locked before saving the
    new password. The account email and every verified address (up to ten) are
    then sent a notice after commit (``accounts.no_password_notices``), once
    per *user* object however many finalize calls the route makes.
    """
    with transaction.atomic():
        lock_user_row(user)
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        user.personal_access_tokens.all().delete()
        revoke_auth_token(user)
        if first_password:
            from .no_password_notices import notify_verified_addresses_of_first_password

            notify_verified_addresses_of_first_password(user)


FIRST_PASSWORD_SESSION_REQUIRED_MESSAGE = (
    "Setting a first password is available from an interactive session only."
)


def first_password_requires_session(request, user) -> bool:
    """True when *request* may not set *user*'s first password (#1609).

    An account with no usable password (social or SSO sign-in only) may set
    one without a current password, but only from an interactive session.
    Fails closed: only a request authenticated by a session (see
    ``request_is_interactive_session``) passes, so a Personal Access Token,
    the DRF auth ``Token`` and any authenticator added later are refused,
    whether or not they report a credential object. A caller that proves a current password is unaffected, so this
    is false whenever ``user.has_usable_password()`` is true.

    Shared by both REST password-change routes
    (``accounts.views.ChangePasswordView`` and
    ``accounts.serializers.VisibanPasswordChangeSerializer``) so the rule
    cannot drift between them. *user* should be the row read under
    ``lock_user_row``.
    """
    if user.has_usable_password():
        return False
    return not request_is_interactive_session(request)


# Stable error code for the SPA (``{"detail": ..., "code": ...}``): it tells
# the user to sign in again rather than showing a generic failure.
FIRST_PASSWORD_RECENT_LOGIN_REQUIRED_CODE = "recent_login_required"
# The reset pointer is for a session with no provider to sign in again with
# (an account without a linked identity): a reset link proves the mailbox.
FIRST_PASSWORD_RECENT_LOGIN_REQUIRED_MESSAGE = (
    "To set a password, sign out, sign in again with your sign-in provider, "
    "and then set the password within a few minutes. If you can't sign in with "
    'a provider, use "Forgot password?" on the sign-in page instead.'
)


def first_password_requires_recent_login(request, user) -> bool:
    """True when *request*'s session is too old to set *user*'s first password (#1260).

    An account with no usable password has no current password to prove, so
    without this check a stolen session cookie alone could set one and keep
    access after the session ends. The owner settled the rule: a first
    password needs a recent sign-in to this account, recorded in this session
    (``session_recently_signed_in_by_provider``). Run after
    ``first_password_requires_session``, which refuses token callers first.
    False whenever ``user.has_usable_password()`` is true: those accounts prove
    their current password instead.
    """
    if user.has_usable_password():
        return False
    return not session_recently_signed_in_by_provider(request, user)


def session_recently_signed_in_by_provider(request, user) -> bool:
    """True when this session holds a recent provider sign-in to *user*'s own account.

    The source is allauth's per-session authentication records
    (``allauth.account.authentication.get_authentication_records``), which its
    social-login flow writes into the session at sign-in time. Not
    ``user.last_login``: that column is per user, so a fresh sign-in on the
    owner's other device would make a stolen, older cookie look recent.

    allauth's own ``did_recently_authenticate`` is not used, because it treats
    an account with no password or MFA as always recently authenticated (it
    has no way to reauthenticate one), which is exactly the account this rule
    is for. The window is allauth's ``ACCOUNT_REAUTHENTICATION_TIMEOUT``
    (default 300 seconds), the same "recent" the registry already uses for
    email changes and provider connects, so there is one setting for it and
    no new environment variable. Five minutes is enough to sign in and fill
    the form, and short enough that a cookie copied from an idle session is
    not usable for this.

    Only a ``socialaccount`` record counts, and only when its provider and uid
    are an identity linked to *user*: an account with no usable password can
    only sign in through a provider. allauth writes a record before the login
    completes, so a sign-in to some other account that stopped part way (for
    example at email verification) can leave its record in the session of the
    user already signed in; matching the identity keeps that from counting.

    Fails closed: a session with no records (one created before this rule, or
    by a REST API login, which records none) or with only older records is
    not recent.
    """
    from allauth.account import app_settings as account_settings
    from allauth.account.authentication import get_authentication_records
    from allauth.socialaccount.models import SocialAccount

    if getattr(request, "session", None) is None:
        return False
    cutoff = time.time() - account_settings.REAUTHENTICATION_TIMEOUT
    recent = set()
    for record in get_authentication_records(request):
        if not isinstance(record, dict) or record.get("method") != "socialaccount":
            continue
        at = record.get("at")
        if not isinstance(at, (int, float)) or at < cutoff:
            continue
        recent.add((record.get("provider"), str(record.get("uid"))))
    if not recent:
        return False
    linked = {
        (provider, str(uid))
        for provider, uid in SocialAccount.objects.filter(user_id=user.pk).values_list(
            "provider", "uid"
        )
    }
    return not recent.isdisjoint(linked)


def request_is_interactive_session(request) -> bool:
    """True when *request* was authenticated by an interactive session.

    The one definition of "interactive session" for the account-credential
    rules that require one (``first_password_requires_session`` above,
    ``email_change_requires_session`` and ``username_change_requires_session``
    below, and ``accounts.views.ConnectedAccountDetailView.delete``,
    ``PendingEmailView.delete`` and ``PendingEmailResendView.post``, which
    call this directly), so they all move together.

    The check is positive. On a DRF request the authenticator that accepted
    the credentials (``request.successful_authenticator``) must be a
    ``SessionAuthentication`` (or a subclass, such as a CSRF-exempt variant),
    and ``request.auth`` must be ``None``. Neither alone is enough: the
    ``None`` test would also pass an authenticator that returns ``(user, None)``,
    and the authenticator test alone would pass a session subclass that also
    sets a credential object. Any other authenticator (a Personal Access Token,
    the DRF auth ``Token``, a test ``force_authenticate``, or one added by an
    extension) is refused without being listed.

    A request that is not a DRF ``Request`` has no authenticator to inspect and
    is refused. In particular the underlying ``request._request`` must not be
    passed here: DRF copies ``auth`` onto it, so an authenticator that returns
    no credential object would look like a session. Pass the DRF request.

    Extension authenticators (``DEFAULT_AUTHENTICATION_CLASSES`` overrides)
    that return ``(user, None)`` are therefore refused by these rules, and an
    extension authenticator must not subclass ``SessionAuthentication`` unless
    it really authenticates from the cookie session.
    """
    if not isinstance(request, Request):
        return False
    if request.auth is not None:
        return False
    return isinstance(request.successful_authenticator, SessionAuthentication)


PROVIDER_DISCONNECT_SESSION_REQUIRED_MESSAGE = (
    "Disconnecting a sign-in provider is available from an interactive session only."
)


EMAIL_CHANGE_SESSION_REQUIRED_MESSAGE = (
    "Changing the email address is available from an interactive session only."
)

EMAIL_CHANGE_PASSWORD_REQUIRED_MESSAGE = (
    "Enter your current password to change your email address."
)
EMAIL_CHANGE_PASSWORD_INCORRECT_MESSAGE = (
    "Your current password was entered incorrectly. Please enter it again."
)


def email_change_requires_current_password(user, requested_email) -> bool:
    """True when changing *user*'s email to *requested_email* needs the current password.

    The account email is where password reset links go, and a reset ends in a
    new password (which also revokes every token). So an email change is
    treated as a credential change: an account that has a usable password must
    prove it knows that password in the same request, the way the password
    change routes do. A signed-in session alone is not enough.

    Accounts without a usable password (sign-in through an identity provider
    only) are not asked: they have no password to confirm, and an owner who
    has lost the primary mailbox must still be able to recover the account.
    Their control is a notice instead: an address added to or confirmed on
    such an account is reported to the account's current email, and a first
    password set (from a reset link or by the signed-in user) is reported to the account email and every
    verified address
    (``accounts.no_password_notices``). The reset gate itself
    (``accounts.forms._ResetLinkGateMixin``) is deliberately left alone:
    tightening it would cut off password reset on ``EMAIL_VERIFICATION=none``
    installs, where no address is ever verified.

    The same "not a change" rule as ``email_change_requires_session``: the
    stored value sent back unchanged needs nothing (and a request that omits
    ``email`` never reaches this check), so clients that round-trip the whole
    profile keep working.
    """
    if (requested_email or "") == (user.email or ""):
        return False
    return user.has_usable_password()


def email_change_requires_session(request, current_email, requested_email) -> bool:
    """True when *request* may not change the account email to *requested_email*.

    Changing the account email, or asking for a change to be confirmed, is
    limited to an interactive session, like setting a first password. A request that omits
    the email, or sends back the stored value unchanged, is not a change: a
    token client that round-trips the whole profile keeps working. Any other
    value, including a case-only difference or a blank, counts as a change.
    *request* may be ``None`` (a serializer used outside a request), which is
    not refused here because no remote credential is involved.
    """
    if request is None:
        return False
    if (requested_email or "") == (current_email or ""):
        return False
    return not request_is_interactive_session(request)


USERNAME_CHANGE_SESSION_REQUIRED_MESSAGE = (
    "Changing the username is available from an interactive session only."
)


def username_change_requires_session(request, current_username, requested_username) -> bool:
    """True when *request* may not change the username to *requested_username*.

    A username is a login identifier, so changing it is limited to an
    interactive session, like the account email
    (``email_change_requires_session``). A request that omits the username, or
    sends back the stored value unchanged, is not a change: a token client that
    round-trips the whole profile keeps working. Any other value, including a
    case-only difference or a blank, counts as a change. *request* may be
    ``None`` (a serializer used outside a request), which is not refused here
    because no remote credential is involved.
    """
    if request is None:
        return False
    if (requested_username or "") == (current_username or ""):
        return False
    return not request_is_interactive_session(request)


def revoke_auth_token(user):
    """Delete *user*'s DRF auth ``Token`` (the key minted at REST login).

    Called from ``finalize_password_change`` (and so from
    ``finalize_password_reset``), from
    ``require_password_change_after_admin_set``,
    ``revoke_credentials_after_operator_reset``, and on account deactivation
    from ``AdminUserDeactivateView._revoke_invite_links``. The SPA
    authenticates with its session cookie, not this key, and REST login mints a
    fresh one on the next sign-in.
    """
    from rest_framework.authtoken.models import Token

    Token.objects.filter(user_id=user.pk).delete()


def reissue_auth_token(user) -> str:
    """Mint a new DRF auth ``Token`` for *user* and return its key.

    Used by the REST password-change views when the request itself was
    authenticated with the ``Token`` that the change just revoked, so that
    caller keeps working with the key returned in the response (#1562). The
    caller runs it after ``finalize_password_change`` in the same transaction;
    it takes the user-row lock itself so it is serialized with every other
    credential write for the user even when called on its own. Any existing
    ``Token`` is replaced, since DRF allows one per user.
    """
    from rest_framework.authtoken.models import Token

    with transaction.atomic():
        lock_user_row(user)
        Token.objects.filter(user_id=user.pk).delete()
        return Token.objects.create(user=user).key


def finalize_password_reset(user, *, first_password=False):
    """Side effects of a completed password reset (REST and HTML).

    A reset is a password change by whoever holds the reset link, so it gets
    the full self-service finalization, which includes the DRF ``Token``
    revocation. No ``Token`` is reissued: the reset caller is not signed in.

    *first_password* is as for ``finalize_password_change``, which sends the
    notice.
    """
    with transaction.atomic():
        lock_user_row(user)
        finalize_password_change(user, first_password=first_password)


def revoke_credentials_after_operator_reset(user):
    """Side effects of an operator resetting a password from the command line.

    The ``changepassword`` management command runs with shell access to the
    deployment, for example to recover an account whose owner cannot sign in.
    Every credential issued under the previous password is revoked (Personal
    Access Tokens and the DRF ``Token``); sessions end on their own because the
    session auth hash is derived from the password hash.

    Unlike ``require_password_change_after_admin_set``, ``must_change_password``
    is left as it is: the operator typed the new password and is usually the
    account owner (the first-boot recovery path), so a forced change would
    only add a second prompt. Idempotent. Run it in the same transaction as the
    password save.
    """
    with transaction.atomic():
        lock_user_row(user)
        user.personal_access_tokens.all().delete()
        revoke_auth_token(user)


def require_password_change_after_admin_set(user):
    """Side effects of an administrator setting another user's password.

    Every credential issued under the previous password is revoked. Sessions
    end on their own: the session auth hash is derived from the password hash.

    When the administrator set a usable password, they now know it, so the
    account owner must choose a new one before continuing (the same rule
    ``AdminUsersView.post`` applies to accounts it creates). When they instead
    disabled password sign-in, the flag is not set: it would make the owner
    create the password the administrator just removed. *user* must carry the
    stored (new) password hash.
    """
    with transaction.atomic():
        lock_user_row(user)
        if user.has_usable_password():
            user.must_change_password = True
            user.save(update_fields=["must_change_password"])
        user.personal_access_tokens.all().delete()
        revoke_auth_token(user)
