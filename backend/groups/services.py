"""Shared group operations that more than one entry point performs.

Keeping the ownership-transfer rules in one function means the REST endpoint
(``GroupViewSet.transfer_ownership``) and the Django admin action
(``groups.admin.GroupAdmin``) cannot drift apart.
"""

from django.db import transaction

from .models import Group, GroupMembership


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
