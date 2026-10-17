"""Django admin for groups: read-only, plus a superuser-only ownership transfer.

``Group.owner`` is CASCADE, and deleting a user who owns groups is refused until
ownership moves (``accounts.admin.user_deletion_refusals``). The owner is the
only account the API lets transfer a group, so a departed or deactivated
owner's groups would otherwise have no route. The "Transfer ownership" action
is that route; it applies the same rules as the API through
``groups.services.transfer_group_ownership``.

Fields are not editable here (and groups cannot be deleted) so the admin does
not offer a way around those rules.
"""

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Value, BooleanField
from django.template.response import TemplateResponse

from accounts.models import User

from . import broadcast as group_broadcast
from .models import Group
from .serializers import GroupSerializer
from .services import GroupTransferError, transfer_group_ownership


class TransferOwnershipForm(forms.Form):
    """Picks the new owner from the active members of one group.

    The queryset is the first check; ``transfer_group_ownership`` judges the
    choice again under row locks when the transfer runs.
    """

    new_owner = forms.ModelChoiceField(queryset=User.objects.none(), label="New owner")

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group
        self.fields["new_owner"].queryset = (
            User.objects.filter(
                is_active=True,
                group_memberships__group=group,
            )
            .exclude(pk=group.owner_id)
            .order_by("username")
        )


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "owner_is_active", "parent", "created_at")
    list_select_related = ("owner", "parent")
    search_fields = ("name", "owner__username", "owner__email")
    list_filter = ("owner__is_active",)
    actions = ["transfer_ownership"]

    @admin.display(
        boolean=True, description="Owner active", ordering="owner__is_active"
    )
    def owner_is_active(self, obj):
        return obj.owner.is_active

    # Superusers only: ownership decides who controls a group's content.
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_transfer_ownership_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def get_actions(self, request):
        # No bulk delete: it would cascade to boards, cards and memberships.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.action(
        description="Transfer ownership of the selected group",
        permissions=["transfer_ownership"],
    )
    def transfer_ownership(self, request, queryset):
        # Defense in depth: ``permissions`` already hides and refuses the action.
        if not self.has_transfer_ownership_permission(request):
            raise PermissionDenied
        if queryset.count() != 1:
            self.message_user(
                request, "Select exactly one group to transfer.", messages.ERROR
            )
            return None
        group = queryset.select_related("owner").get()

        confirmed = request.POST.get("step") == "confirm"
        form = TransferOwnershipForm(request.POST if confirmed else None, group=group)
        if confirmed and form.is_valid():
            new_owner = form.cleaned_data["new_owner"]
            try:
                with transaction.atomic():
                    updated = transfer_group_ownership(
                        group.pk,
                        new_owner.pk,
                        expected_owner_id=group.owner_id,
                        require_admin_role=False,
                        require_active=True,
                    )
                    self._broadcast_after_commit(updated.pk)
            except GroupTransferError as exc:
                self.message_user(request, str(exc), messages.ERROR)
                return None
            self.message_user(
                request,
                f'Ownership of "{group.name}" transferred to {new_owner}.',
                messages.SUCCESS,
            )
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": "Transfer ownership",
            "opts": self.model._meta,
            "group": group,
            "form": form,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        }
        return TemplateResponse(
            request, "admin/groups/group/transfer_ownership.html", context
        )

    def _broadcast_after_commit(self, group_id):
        """Send the same ``group.updated`` event the API transfer sends.

        Queued with ``transaction.on_commit`` so clients never see an owner the
        database does not have. The payload carries no per-viewer star state.
        """
        group = (
            Group.objects.select_related("owner", "parent")
            .annotate(
                _member_count=Count("memberships", distinct=True),
                _board_count=Count("boards", distinct=True),
                _subgroup_count=Count("subgroups", distinct=True),
                _is_starred=Value(False, output_field=BooleanField()),
            )
            .get(pk=group_id)
        )
        data = GroupSerializer(group).data

        def _send(gid=group_id, payload=data):
            group_broadcast.broadcast_group_event(
                gid, group_broadcast.EVT_GROUP_UPDATED, payload
            )

        transaction.on_commit(_send)
