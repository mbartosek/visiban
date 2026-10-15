import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def normalize_roles(apps, schema_editor):
    """Map any stored role outside the allowed set to the least-privileged role.

    Rows are set to ``viewer`` with the moderator flag cleared, so a row of
    unknown provenance never gains more access than it had a defined meaning for.
    """
    BoardMembership = apps.get_model("boards", "BoardMembership")
    allowed = ["admin", "member", "collaborator", "viewer"]
    bad = BoardMembership.objects.exclude(role__in=allowed)
    pairs = sorted(bad.values_list("board_id", "user_id"))
    if pairs:
        # Ids only, so operators keep an audit trail without PII in logs.
        logger.warning(
            "Normalizing %d board membership row(s) with an invalid role "
            "to viewer; (board_id, user_id): %s",
            len(pairs),
            pairs,
        )
    bad.update(role="viewer", is_moderator=False)


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0070_boardexportlog_row_count_help_text"),
    ]

    operations = [
        migrations.RunPython(normalize_roles, migrations.RunPython.noop),
    ]
