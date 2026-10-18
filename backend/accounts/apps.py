from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import checks  # noqa: F401 — registers the #1337 system check

        from .signals import _connect_user_signals
        _connect_user_signals()

        from allauth.account.signals import email_added, email_confirmed

        from .email_change import apply_confirmed_email_change

        from django.contrib.auth.signals import user_logged_in, user_logged_out

        from .social_connect import clear_pending_connect_on_login, clear_pending_connect_on_logout

        user_logged_in.connect(
            clear_pending_connect_on_login,
            dispatch_uid="accounts.clear_pending_connect_on_login",
        )
        user_logged_out.connect(
            clear_pending_connect_on_logout,
            dispatch_uid="accounts.clear_pending_connect_on_logout",
        )

        # Under EMAIL_VERIFICATION=mandatory, a group or board invite used to
        # register with a not-yet-verified invited address grants its
        # membership when that address is confirmed (#1704). Connected first:
        # allauth sends email_confirmed with a plain send(), so a receiver that
        # raises stops the ones after it. This one never raises, and running it
        # first means a failure in the email-change receiver cannot leave an
        # invite waiting on an address that is already verified.
        from visiban.invite_recipient import grant_reserved_invites

        email_confirmed.connect(
            grant_reserved_invites,
            dispatch_uid="visiban.invite_recipient.grant_reserved_invites",
        )
        # Accounts without a usable password: an added or confirmed address
        # is reported to the account's current email. Connected ahead of the
        # email-change receiver so the recipient is read before a confirmed
        # change moves User.email; it never raises.
        from .no_password_notices import notify_on_email_added, notify_on_email_confirmed

        email_added.connect(
            notify_on_email_added,
            dispatch_uid="accounts.no_password_notices.notify_on_email_added",
        )
        email_confirmed.connect(
            notify_on_email_confirmed,
            dispatch_uid="accounts.no_password_notices.notify_on_email_confirmed",
        )
        email_confirmed.connect(
            apply_confirmed_email_change,
            dispatch_uid="accounts.apply_confirmed_email_change",
        )

        # allauth's HTML password views finalize through the same helpers as
        # the REST routes (#1551).
        from .password_signals import connect_password_signals

        connect_password_signals()
