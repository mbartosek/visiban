"""allauth password signal receivers (#1551).

allauth's HTML views (its change and set views, whose project routes go to
the SPA, #1561, and ``accounts/password/reset/key/...``) save the password through the
project's forms in ``accounts.forms``, which apply the follow-up from
``accounts.credentials`` in the same transaction as the save. After the save,
allauth sends ``password_changed``, ``password_set`` or ``password_reset``;
these receivers run the same idempotent helpers again as a backstop, so a
password-setting form that does not finalize still ends in the same state.
Connected in ``AccountsConfig.ready``.

Defense in depth (#1554): the project's URLconf now redirects all of these
pages to the SPA, so the receivers are not reached in a default install. They
stay on purpose: an extension or test URLconf can mount allauth's own views,
and the receivers keep any password-setting path that does emit the signals
ending in the same finalized state. Do not remove them as dead code.

The REST routes do not send these signals: dj-rest-auth's serializers save
through Django's ``SetPasswordForm``, and ``ChangePasswordView`` calls
``set_password`` directly (``accounts/tests/test_forced_change_html_surfaces.py``
pins this). A receiver that raises is not caught, so the request fails.
"""

from __future__ import annotations

from . import credentials


def finalize_on_password_changed(sender, request, user, **kwargs):
    """allauth ``password_changed``: the user changed their own password."""
    credentials.finalize_password_change(user)


def finalize_on_password_set(sender, request, user, **kwargs):
    """allauth ``password_set``: a password-less (social-only) user set one.

    The signal is sent only for an account's first password, so the notice
    applies; it is sent once per user object, whichever route finalized first.
    """
    credentials.finalize_password_change(user, first_password=True)


def finalize_on_password_reset(sender, request, user, **kwargs):
    """allauth ``password_reset``: a password was set from a reset link.

    The password is already saved here, so whether it was the account's first
    is no longer known; the project's reset form passes that to
    ``finalize_password_reset`` itself, and this backstop does not repeat the
    first-password notice. That holds because every reset in a default
    install goes through the project's REST reset-confirm serializer
    (``PASSWORD_RESET_CONFIRM_SERIALIZER``) or, where allauth's key page is
    mounted, its form (``ACCOUNT_FORMS["reset_password_from_key"]``); the
    project URLconf redirects allauth's key page to the SPA.
    ``accounts/tests/test_no_password_account_notices.py`` pins all three.
    """
    credentials.finalize_password_reset(user)


def connect_password_signals():
    from allauth.account.signals import password_changed, password_reset, password_set

    password_changed.connect(
        finalize_on_password_changed,
        dispatch_uid="accounts.finalize_on_password_changed",
    )
    password_set.connect(
        finalize_on_password_set,
        dispatch_uid="accounts.finalize_on_password_set",
    )
    password_reset.connect(
        finalize_on_password_reset,
        dispatch_uid="accounts.finalize_on_password_reset",
    )
