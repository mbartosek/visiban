from django.db import migrations

ALLOWED = ["viewer", "collaborator", "member", "admin"]


def normalize_export_min_role(apps, schema_editor):
    """Map a stored export_min_role outside the allowed set to ``admin``.

    The export gate treats an unrecognized value as admin-only, so this keeps
    the effective behavior and gives the admin form (which now renders a
    select) a selected option to round-trip, rather than resubmitting the
    first option on an unrelated edit. A blank value means the default
    (``viewer``) to the gate and is left as it is.
    """
    Board = apps.get_model("boards", "Board")
    Board.objects.exclude(export_min_role__in=ALLOWED).exclude(export_min_role="").update(
        export_min_role="admin"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0085_board_export_min_role_choices"),
    ]

    operations = [
        migrations.RunPython(normalize_export_min_role, migrations.RunPython.noop),
    ]
