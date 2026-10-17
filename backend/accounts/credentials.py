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

from django.contrib.auth import get_user_model
from django.db import transaction


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


def finalize_password_change(user):
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
    """
    with transaction.atomic():
        lock_user_row(user)
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        user.personal_access_tokens.all().delete()
        revoke_auth_token(user)


FIRST_PASSWORD_SESSION_REQUIRED_MESSAGE = (
    "Setting a first password is available from an interactive session only."
)


def first_password_requires_session(request, user) -> bool:
    """True when *request* may not set *user*'s first password (#1609).

    An account with no usable password (social or SSO sign-in only) may set
    one without a current password, but only from an interactive session.
    Fails closed: a session leaves ``request.auth`` as ``None``, so any
    request that carries a non-``None`` credential object (a Personal Access
    Token, the DRF auth ``Token``, or an authenticator added later) is
    refused. A caller that proves a current password is unaffected, so this
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


def request_is_interactive_session(request) -> bool:
    """True when *request* was authenticated by an interactive session.

    The one definition of "interactive session" for the account-credential
    rules that require one (``first_password_requires_session`` above and
    ``email_change_requires_session`` below). DRF's ``SessionAuthentication``
    leaves ``request.auth`` as ``None``; the token authenticators configured
    in ``DEFAULT_AUTHENTICATION_CLASSES`` (a Personal Access Token, the DRF
    auth ``Token``) set it to the credential object. A token authenticator
    added later is refused here without being listed, provided it follows the
    DRF convention of returning its credential as ``request.auth``; one that
    returns ``(user, None)`` would be treated as a session, so such an
    authenticator must not be added without revisiting this check.
    """
    return getattr(request, "auth", None) is None


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
    only) are not asked: there is no password to give, and a reset link for
    such an account is only ever sent to an address already verified on it
    (``accounts.tokens.AddressBoundTokenGenerator``), so the stored email is
    not what decides where a link can go. The reset gate itself
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


def finalize_password_reset(user):
    """Side effects of a completed password reset (REST and HTML).

    A reset is a password change by whoever holds the reset link, so it gets
    the full self-service finalization, which includes the DRF ``Token``
    revocation. No ``Token`` is reissued: the reset caller is not signed in.
    """
    with transaction.atomic():
        lock_user_row(user)
        finalize_password_change(user)


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
