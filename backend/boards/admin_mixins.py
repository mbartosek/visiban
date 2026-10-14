"""Django admin mixins shared by ``boards.admin`` and ``accounts.admin``."""

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse

from .services.errors import ContainerChanged


class ContainerChangedAdminMixin:
    """Answer ``ContainerChanged`` (#1602) with an error message, not a 500.

    A column, swimlane, board or user delete raises it, with nothing deleted,
    when cards kept entering the container through every bounded retry of
    ``lock_cards_for_cascade``. It is caught at the view level, not in
    ``delete_model``/``delete_queryset``: Django prints its "deleted
    successfully" message after those return, so catching there would report
    a delete that did not happen.

    * Single delete: ``delete_view`` already runs in ``transaction.atomic()``,
      so the deletion log entry rolls back with the delete; redirect to the
      object's change page.
    * Bulk ``delete_selected``: Django writes the log entries *before*
      ``delete_queryset`` and outside any transaction, so the action is
      wrapped in one here to roll them back too; redirect to the changelist.
    """

    def delete_view(self, request, object_id, extra_context=None):
        try:
            return super().delete_view(request, object_id, extra_context)
        except ContainerChanged as exc:
            messages.error(request, exc.detail)
            opts = self.opts
            return HttpResponseRedirect(
                reverse(
                    f"admin:{opts.app_label}_{opts.model_name}_change",
                    args=[object_id],
                    current_app=self.admin_site.name,
                )
            )

    def response_action(self, request, queryset):
        if request.POST.get("action") != "delete_selected":
            return super().response_action(request, queryset)
        try:
            with transaction.atomic():
                return super().response_action(request, queryset)
        except ContainerChanged as exc:
            messages.error(request, exc.detail)
            return HttpResponseRedirect(request.get_full_path())
