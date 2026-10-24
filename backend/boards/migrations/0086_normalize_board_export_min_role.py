from django.db import migrations

ALLOWED = ["viewer", "collaborator", "member", "admin"]


def normalize_export_min_role(apps, schema_editor):
    """Normalize a stored export_min_role to a member of the allowed set.

    A blank value means the default (``viewer``) to the export gate, so it
    becomes ``viewer`` with no change in behavior. Any other value outside the
    set becomes ``admin``, which is what the gate now applies; on a board
    that held such a value before this release, export narrows from everyone
    with read access to admins only. Both make the published enum accurate
    and give the admin form (a select) a selected option to round-trip.
    """
    Board = apps.get_model("boards", "Board")
    Board.objects.filter(export_min_role="").update(export_min_role="viewer")
    Board.objects.exclude(export_min_role__in=ALLOWED).update(export_min_role="admin")


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0085_board_export_min_role_choices"),
    ]

    operations = [
        migrations.RunPython(normalize_export_min_role, migrations.RunPython.noop),
    ]
