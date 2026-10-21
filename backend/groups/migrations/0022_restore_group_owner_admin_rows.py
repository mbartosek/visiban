from django.db import migrations
from django.db.models import Exists, OuterRef
from django.utils import timezone

_CHUNK = 500


def restore_owner_admin_rows(apps, schema_editor):
    """Give every group's owner a direct ``admin`` membership row (#1786).

    A group's owner holds a direct admin membership: the group is created
    with one, ownership transfer requires and keeps one, and the member
    guards refuse removing or demoting it. A group whose owner row was
    removed or demoted before those guards existed is brought back to that
    shape here: an existing non-admin row is promoted, a missing row is
    created. No other membership is touched.

    A data migration, not a management command, so every install reaches the
    documented state on upgrade without an operator step. Idempotent: a
    group that already has the row is skipped, so a re-run writes nothing.
    Data-only; reverse is a no-op because a restored row is indistinguishable
    from one the group was created with, and removing it would break the
    rule the application relies on.
    """
    Group = apps.get_model("groups", "Group")
    GroupMembership = apps.get_model("groups", "GroupMembership")

    owner_is_admin = GroupMembership.objects.filter(
        group_id=OuterRef("pk"), user_id=OuterRef("owner_id"), role="admin"
    )
    pairs = list(
        Group.objects.exclude(Exists(owner_is_admin))
        .order_by("pk")
        .values_list("pk", "owner_id")
    )
    for start in range(0, len(pairs), _CHUNK):
        chunk = pairs[start:start + _CHUNK]
        existing = set()
        for group_id, owner_id in chunk:
            promoted = GroupMembership.objects.filter(
                group_id=group_id, user_id=owner_id
            ).update(role="admin")
            if promoted:
                existing.add(group_id)
        now = timezone.now()
        GroupMembership.objects.bulk_create(
            [
                GroupMembership(group_id=group_id, user_id=owner_id, role="admin", joined_at=now)
                for group_id, owner_id in chunk
                if group_id not in existing
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("groups", "0021_group_invite_reserved_for_idx"),
    ]

    operations = [
        migrations.RunPython(restore_owner_admin_rows, migrations.RunPython.noop),
    ]
