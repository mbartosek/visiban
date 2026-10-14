from django.db import migrations


def backfill_superuser_flags(apps, schema_editor):
    """Set the derived admin flags on existing superusers in one visible step.

    The application treats a superuser as a site admin with all-content access;
    this normalizes stored rows to match. Uses the historical model, so no
    signals fire, and is idempotent.
    """
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=True).exclude(
        is_site_admin=True, can_access_all_content=True
    ).update(is_site_admin=True, can_access_all_content=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0035_invite_link_delivery"),
    ]

    operations = [
        migrations.RunPython(backfill_superuser_flags, migrations.RunPython.noop),
    ]
