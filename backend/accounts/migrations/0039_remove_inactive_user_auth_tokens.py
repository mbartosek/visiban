from django.db import migrations


def remove_inactive_user_auth_tokens(apps, schema_editor):
    """One-time cleanup of API auth tokens belonging to inactive accounts (#1674).

    Applies the deactivation rule (an inactive account holds no API credential)
    to accounts that were deactivated before it covered the auth token. Data
    only; reverse is a no-op because deleted tokens cannot be restored, and a
    fresh key is issued at the next sign-in.
    """
    Token = apps.get_model("authtoken", "Token")
    Token.objects.filter(user__is_active=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0038_db_default_not_null_columns"),
        ("authtoken", "0004_alter_tokenproxy_options"),
    ]

    operations = [
        migrations.RunPython(remove_inactive_user_auth_tokens, migrations.RunPython.noop),
    ]
