from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import checks  # noqa: F401 — registers the #1337 system check

        from .signals import _connect_user_signals
        _connect_user_signals()

        from allauth.account.signals import email_confirmed

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

        email_confirmed.connect(
            apply_confirmed_email_change,
            dispatch_uid="accounts.apply_confirmed_email_change",
        )

        # Under EMAIL_VERIFICATION=mandatory, a group or board invite used to
        # register with a not-yet-verified invited address grants its
        # membership when that address is confirmed (#1704).
        from visiban.invite_recipient import grant_reserved_invites

        email_confirmed.connect(
            grant_reserved_invites,
            dispatch_uid="visiban.invite_recipient.grant_reserved_invites",
        )

        # allauth's HTML password views finalize through the same helpers as
        # the REST routes (#1551).
        from .password_signals import connect_password_signals

        connect_password_signals()
