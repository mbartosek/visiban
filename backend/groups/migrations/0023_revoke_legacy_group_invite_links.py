from datetime import timedelta

from django.db import migrations
from django.utils import timezone

# Matches the default ``expiry_days`` a new shareable link gets (#1727).
_CAP_DAYS = 7


def revoke_legacy_group_invite_links(apps, schema_editor):
    """Close the pre-#1727 hole in shareable group invite links (#1940).

    #1727 stopped new shareable (``delivery='link'``) links from granting
    admin or living forever, but rows minted before it kept redeeming. A
    shareable link is a bearer credential and a group admin is board admin
    across the group's subtree, so on upgrade:

    - an active, unredeemed ``admin`` link is revoked (``is_active=False``),
      not silently downgraded: the creator chose admin, and a member link is
      a different grant they did not ask for;
    - an active, unredeemed link with no expiry gets one ``_CAP_DAYS`` from
      now, the same as a newly created link.

    Emailed invites (``delivery='email'``) are bound to an address and are
    not touched. Idempotent: a re-run finds nothing left to change. Reverse
    is a no-op because the revoked and capped rows cannot be told apart from
    ones an admin changed by hand, and restoring admin power is not wanted.
    """
    GroupInviteLink = apps.get_model("groups", "GroupInviteLink")
    legacy = GroupInviteLink.objects.filter(
        delivery="link", is_active=True, used_at__isnull=True,
    )
    legacy.filter(role="admin").update(is_active=False)
    legacy.filter(expires_at__isnull=True).update(
        expires_at=timezone.now() + timedelta(days=_CAP_DAYS),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("groups", "0022_restore_group_owner_admin_rows"),
    ]

    operations = [
        migrations.RunPython(revoke_legacy_group_invite_links, migrations.RunPython.noop),
    ]
