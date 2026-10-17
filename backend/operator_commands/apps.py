from django.apps import AppConfig


class OperatorCommandsConfig(AppConfig):
    """Holds management commands that replace Django's built-in ones.

    Django resolves a command name to the first app in ``INSTALLED_APPS`` that
    provides it, and ``django.contrib.auth`` ships ``changepassword``. This app
    is listed before it for that reason, and is kept separate from ``accounts``
    so that moving it up does not change template or static-file precedence
    (``accounts`` ships templates that sit below allauth's in the search order).
    It has no models, templates or URLs.
    """

    name = "operator_commands"
    default_auto_field = "django.db.models.BigAutoField"
