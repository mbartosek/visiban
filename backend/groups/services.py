"""Shared group operations that more than one entry point performs.

Keeping the ownership-transfer rules in one function means the REST endpoint
(``GroupViewSet.transfer_ownership``) and the Django admin action
(``groups.admin.GroupAdmin``) cannot drift apart.
"""

from django.db import transaction

from .models import Group, GroupMembership


def lock_group_subtree_for_delete(group_id):
    """Lock ``group_id`` and every subgroup under it ``FOR UPDATE``; return ``(owner_id, parent_id)``.

    Returns ``None`` when the group is already gone (a delete that queued
    behind another delete of the same group). Must run inside
    ``transaction.atomic()``, before ``group.delete()``.

    Why (#1638): the delete collector reads the group's memberships, then
    reaches the invite rows at its ``DELETE``, and removes the group row last.
    Without the group row locked first, an invite redemption (join,
    invite-only registration, the confirmation grant) holding an invite row
    made the delete wait there and then commit a membership the collector had
    already read past, so the delete failed its deferred foreign key at COMMIT
    (a 500), or deadlocked against a grant that holds the group row and waits
    on the invite. Every redemption takes the group row ``FOR NO KEY UPDATE``
    (``groups.invite_registration.lock_invite_group``) before the invite row,
    which conflicts with this ``FOR UPDATE``, so a redemption and a delete
    now queue whole in either order.

    The subgroups are locked too because the delete cascades to them, and an
    invite on a subgroup races a parent delete the same way. All rows are
    locked in one pk-ordered statement, the order the parent-change writer
    (``GroupSerializer``) locks a chain in, so the two cannot deadlock. The
    owner and parent are returned from the locked row: the caller re-checks
    ownership against it (a transfer may have committed while the delete
    queued) and broadcasts to the parent the group actually has.
    """
    ids, frontier = {group_id}, [group_id]
    while frontier:
        frontier = list(
            Group.objects.filter(parent_id__in=frontier).exclude(pk__in=ids).values_list("pk", flat=True)
        )
        ids.update(frontier)
    rows = {
        pk: (owner_id, parent_id)
        for pk, owner_id, parent_id in Group.objects.select_for_update()
        .filter(pk__in=ids).order_by("pk").values_list("pk", "owner_id", "parent_id")
    }
    return rows.get(group_id)


class GroupTransferError(Exception):
    """A transfer was refused; ``str(exc)`` is the user-facing message."""


def transfer_group_ownership(
    group_id,
    new_owner_id,
    *,
    expected_owner_id=None,
    require_admin_role=True,
    require_active=False,
):
    """Make ``new_owner_id`` the owner of the group and keep the previous owner as admin.

    Judged under row locks (the group row, then the new owner's membership) so
    the owner and the membership the rules read cannot change between the check
    and the write. ``expected_owner_id`` makes the transfer refuse if the owner
    is no longer who the caller authorized.

    ``require_admin_role=True`` is the owner-initiated rule: the new owner must
    already be an admin of the group. The administrator route passes ``False``
    and ``require_active=True``: a departed owner's group may have no other
    admin, so any active member can be chosen and is made an admin member, which
    keeps the invariant that an owner holds the admin role.

    Returns the updated group. Callers that broadcast must register their
    ``transaction.on_commit`` callback inside their own ``atomic`` block.
    """
    with transaction.atomic():
        group = Group.objects.select_for_update().get(pk=group_id)
        if expected_owner_id is not None and group.owner_id != expected_owner_id:
            raise GroupTransferError("Only the group owner can transfer ownership.")

        try:
            membership = (
                GroupMembership.objects.select_for_update()
                .select_related("user")
                .get(group=group, user_id=new_owner_id)
            )
        except GroupMembership.DoesNotExist:
            raise GroupTransferError(
                "New owner must already be a group member."
            ) from None

        if require_active and not membership.user.is_active:
            raise GroupTransferError("New owner must be an active user.")
        if require_admin_role and membership.role != GroupMembership.Role.ADMIN:
            raise GroupTransferError("New owner must be an admin of this group.")

        previous_owner_id = group.owner_id
        group.owner_id = new_owner_id
        group.save(update_fields=["owner_id"])
        if membership.role != GroupMembership.Role.ADMIN:
            membership.role = GroupMembership.Role.ADMIN
            membership.save(update_fields=["role"])
        # The previous owner stays on the group as an admin member.
        GroupMembership.objects.update_or_create(
            group=group,
            user_id=previous_owner_id,
            defaults={"role": GroupMembership.Role.ADMIN},
        )
        return group
