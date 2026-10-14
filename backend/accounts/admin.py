import functools
import json

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group as AuthGroup
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import (
    AdminActionLog,
    SiteSetting,
    User,
    record_site_setting_changes,
    snapshot_site_setting,
)
from boards.models import Board, Card, Column
from boards.admin_mixins import ContainerChangedAdminMixin
from boards.services.cards import lock_cards_for_cascade

from .credentials import finalize_password_change, require_password_change_after_admin_set


#: Fields only a superuser may edit in the Django admin user form (#1577).
_SUPERUSER_ONLY_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")


#: Hidden field carrying the account's state as the change form rendered it (#1622).
ROW_VERSION_FIELD = "row_version"

STALE_FORM_MESSAGE = (
    "This account was changed after this page was opened. Reload the page and "
    "make your changes again."
)

#: Shown after a redirect to the change page, which then shows the current state.
STALE_SAVE_MESSAGE = (
    "This account was changed after this page was opened, so nothing was saved. "
    "The page now shows the current state of the account; make your changes again."
)


class StaleSubmission(Exception):
    """The submitted ``row_version`` does not match the row read under the lock."""


def _m2m_state(user, field_names):
    """Sorted primary keys of *user*'s many-to-many fields among *field_names*."""
    state = {}
    for name in sorted(field_names):
        if User._meta.get_field(name).many_to_many:
            # Read from the database, not a prefetch cache.
            state[name] = sorted(getattr(user, name).values_list("pk", flat=True))
    return state


def user_row_version(user, field_names):
    """A keyed digest of *user*'s stored values for the form fields *field_names*.

    The change form renders it as ``row_version``; a submission is accepted
    only while it still matches the stored account (#1622). The digest is
    keyed with ``SECRET_KEY`` so the hidden value says nothing about the
    stored values.
    """
    state = {"pk": user.pk, **_m2m_state(user, field_names)}
    for name in field_names:
        field = User._meta.get_field(name)
        if not field.many_to_many:
            state[name] = field.value_from_object(user)
    payload = json.dumps(state, sort_keys=True, default=str)
    return salted_hmac("accounts.admin.user_row_version", payload).hexdigest()


def row_version_matches(submitted, current):
    return constant_time_compare(submitted, current)


class _RowVersionField(forms.CharField):
    """Hidden ``row_version``; never reported as a changed field.

    It is not account data, so it stays out of ``changed_data`` and the admin
    history's change message.
    """

    widget = forms.HiddenInput

    def has_changed(self, initial, data):
        return False


class VisibanUserChangeForm(UserChangeForm):
    """The user change form with the admin API's account guards.

    ``VisibanUserAdmin.get_form`` sets ``guard_actor`` to the requesting user.
    The guards apply to every actor: deactivating one's own account, a board
    owner or the last active site admin, and removing one's own or the last
    active superuser status, are refused. Refusals are form errors so they
    render on the form and nothing is saved.

    ``row_version`` records the account's stored values, for the fields this
    form writes, as the form rendered them (#1622). A submission made against
    an older state of the account is refused:

    - ``clean`` compares it with the row read when the submission arrives and
      refuses with a form error asking for a reload;
    - ``VisibanUserAdmin.save_model`` compares it again with the row (and
      its groups and permissions) read under the row lock, and
      ``save_related`` re-reads the groups and permissions before writing
      them; a mismatch there rolls the save back and redirects to the change
      page with an error message.

    A submission without ``row_version`` (not produced by the rendered form)
    skips both comparisons; it carries no privilege, since the same
    administrator could submit any values. Such a submission still keeps the
    stored value of every field the administrator left alone.
    """

    guard_actor = None

    row_version = _RowVersionField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stored_row_version = None
        if self.instance.pk and ROW_VERSION_FIELD in self.fields:
            self._stored_row_version = user_row_version(self.instance, self._versioned_fields())
            self.fields[ROW_VERSION_FIELD].initial = self._stored_row_version

    def submitted_row_version(self):
        """The submitted ``row_version``, or ``None`` when absent or unbound."""
        if not self.is_bound:
            return None
        return getattr(self, "cleaned_data", {}).get(ROW_VERSION_FIELD) or None

    def _versioned_fields(self):
        """The model fields this form can write (disabled fields write nothing)."""
        model_fields = {f.name for f in User._meta.get_fields() if f.concrete}
        return [
            name for name, field in self.fields.items()
            if name in model_fields and not field.disabled
        ]

    def clean(self):
        cleaned = super().clean()
        submitted = cleaned.get(ROW_VERSION_FIELD)
        if (
            self.is_bound
            and submitted
            and self._stored_row_version is not None
            and not row_version_matches(submitted, self._stored_row_version)
        ):
            raise ValidationError(STALE_FORM_MESSAGE, code="stale_form")
        actor = self.guard_actor
        stored = self.instance  # clean() runs before the instance is updated
        if actor is None or not stored.pk:
            return cleaned
        from .admin_views import deactivation_refusal, demotion_refusal

        if stored.is_active and "is_active" in self.fields and cleaned.get("is_active") is False:
            refusal = deactivation_refusal(stored, actor, include_last_site_admin=True)
            if refusal is not None:
                raise ValidationError(_refusal_message(refusal), code="deactivation_refused")
        if stored.is_superuser and "is_superuser" in self.fields and cleaned.get("is_superuser") is False:
            refusal = demotion_refusal(stored, actor)
            if refusal is not None:
                raise ValidationError(_refusal_message(refusal), code="demotion_refused")
        return cleaned


def _is_protected_other_account(actor, target):
    """True for a staff, superuser or site-admin account other than *actor*'s own.

    The Django admin's account-changing and delete routes are superuser-only
    for these accounts: a staff account's Django permissions (groups, user
    permissions) are not comparable between actor and target, so no
    "broader than the actor" test is attempted and the whole class stays with
    superusers.
    """
    return (
        target is not None
        and target.pk != actor.pk
        and (target.is_staff or target.is_superuser or target.is_site_admin)
    )


def _refusal_message(refusal):
    # Board names stay out of the form message; the count is enough here.
    message = refusal.body["detail"]
    boards = refusal.body.get("owned_boards")
    if boards:
        message += f" Owned boards: {len(boards)}."
    return message


class DeletionRefused(Exception):
    """A user delete was refused; ``messages`` are shown to the administrator (#1616)."""

    def __init__(self, messages_):
        super().__init__("; ".join(messages_))
        self.messages = list(messages_)


def user_deletion_refusals(targets, actor):
    """Reasons *targets* may not be deleted by *actor*, as admin-facing strings.

    Deleting a user cascades to the boards they own (``Board.owner`` is
    CASCADE), so the deactivation rules apply to a delete as well: no
    self-deletion and no deleting a board owner before the boards are
    transferred (``deactivation_refusal``, the rule the form and the admin API
    share), and never the last active site admin or the last active
    superuser, mirroring ``demotion_refusal`` (#1616).

    A bulk delete is judged as a set: each account is checked on its own, and
    the active site admins and superusers left outside the set must be
    non-empty, because two accounts that are each "not the last" can be the
    last two. The same rules apply to every actor, superusers included, as on
    the change form. ``delete_model`` and ``delete_queryset`` call this twice
    under row locks (see ``VisibanUserAdmin._lock_and_check_admins`` and
    ``_lock_cards_then_targets``): the administrator flags and counts cannot
    move once the first lock is held, and the owned-boards rule is judged
    again after the target rows are locked ``FOR UPDATE``. Board names are not
    shown, only a count, as on the form.

    Deleting a user also cascades to the groups they own (``Group.owner`` is
    CASCADE), so ownership of every owned group is transferred first, and a
    non-superuser actor may delete only accounts that are not another staff,
    superuser or site-admin account (``_is_protected_other_account``).
    """
    from .admin_views import deactivation_refusal

    targets = list(targets)
    reasons = []
    for target in targets:
        refusal = deactivation_refusal(target, actor)
        if refusal is None:
            continue
        if refusal.body.get("code") == "owned_boards":
            count = len(refusal.body.get("owned_boards", []))
            reason = (
                "User owns boards that must be transferred before deletion "
                f"(owned boards: {count})."
            )
        elif target.pk == actor.pk:
            reason = "You cannot delete your own account."
        else:
            reason = "This account cannot be deleted."
        reasons.append(f"{target}: {reason}")
    from groups.models import Group

    for target in targets:
        if not actor.is_superuser and _is_protected_other_account(actor, target):
            reasons.append(f"{target}: Only a superuser can delete this account.")
        # Group.owner is CASCADE: the groups, their subgroups and memberships
        # would go with the owner, so ownership is transferred first.
        group_count = Group.objects.filter(owner=target).count()
        if group_count:
            reasons.append(
                f"{target}: User owns groups that must be transferred before "
                f"deletion (owned groups: {group_count})."
            )
    target_pks = {t.pk for t in targets}
    single = len(targets) == 1
    for flag, singular, plural in (
        ("is_site_admin", "the last active site admin", "every active site admin"),
        ("is_superuser", "the last active superuser", "every active superuser"),
    ):
        if any(getattr(t, flag) and t.is_active for t in targets):
            outside = User.objects.filter(**{flag: True, "is_active": True}).exclude(
                pk__in=target_pks
            )
            if not outside.exists():
                reasons.append(f"Cannot delete {singular if single else plural}.")
    return reasons


def guarded_delete_selected(modeladmin, request, queryset):
    """The ``delete_selected`` action with the user-delete guards applied (#1616).

    Atomic by design: when any selected account is refused, nothing is deleted
    and every reason is shown, so a partial delete never leaves the
    administrator guessing what happened. Checked before the confirmation page
    and again on the confirming POST. The stock action runs in one
    transaction so the deletion log entries Django writes roll back with a
    refusal raised under the row lock.
    """
    if not modeladmin.has_delete_permission(request):
        raise PermissionDenied
    reasons = user_deletion_refusals(queryset, request.user)
    if not reasons:
        try:
            with transaction.atomic():
                return delete_selected(modeladmin, request, queryset)
        except DeletionRefused as exc:
            reasons = exc.messages
    for reason in reasons:
        modeladmin.message_user(request, reason, messages.ERROR)
    modeladmin.message_user(request, "No accounts were deleted.", messages.ERROR)
    return None


functools.update_wrapper(guarded_delete_selected, delete_selected)


@admin.register(User)
class VisibanUserAdmin(ContainerChangedAdminMixin, UserAdmin):
    """Django's ``UserAdmin`` with the project's password rules applied (#1551).

    A password set through the admin's per-user password form has the same
    follow-up as every other route (registry Rule 3): see
    ``user_change_password``. A user created here must choose their own
    password on first use, as with ``AdminUsersView.post``.

    The admin's own "change my password" page (``/admin/password_change/``) is
    routed to the SPA in ``visiban/urls.py`` instead.
    """

    form = VisibanUserChangeForm

    def get_queryset(self, request):
        """Non-superuser staff do not see superuser accounts."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(is_superuser=False)
        return qs

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Turn a refusal under the row lock into a message, not a server error.

        ``StaleSubmission`` propagates out of Django's transaction around the
        change view, so everything the save wrote is rolled back; the
        administrator is sent back to the change page, which shows the
        account's current state (#1622).
        """
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except StaleSubmission:
            self.message_user(request, STALE_SAVE_MESSAGE, messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())

    def get_fieldsets(self, request, obj=None):
        """The change form also carries the hidden ``row_version`` (#1622).

        A view-only page has no form to submit, and Django renders every field
        there as read-only text, so the field is left out.
        """
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None or not fieldsets or not self.has_change_permission(request, obj):
            return fieldsets
        (name, options), *rest = fieldsets
        return [(name, {**options, "fields": (*options["fields"], ROW_VERSION_FIELD)}), *rest]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.guard_actor = request.user
        return form

    def _refuse_privileged_password_target(self, request, id):
        """Only a superuser sets the password of a staff, superuser or site-admin account.

        The lookup bypasses ``get_queryset`` so a superuser target is refused
        explicitly rather than reported as missing. An account's own password
        form stays available to its owner.
        """
        if request.user.is_superuser:
            return
        try:
            target = (
                admin.ModelAdmin.get_queryset(self, request)
                .filter(pk=unquote(id))
                .only("pk", "is_staff", "is_superuser", "is_site_admin")
                .first()
            )
        except (ValueError, ValidationError):
            return
        if _is_protected_other_account(request.user, target):
            raise PermissionDenied

    def user_change_password(self, request, id, form_url=""):
        """Run Django's view, then apply the password follow-up if it set one.

        Django's view is not reimplemented: whether a password was set is
        decided by comparing the stored hash before and after it runs, not by
        its (translated) success message. GET, an invalid form and a refused
        request leave the hash unchanged and so have no side effects.

        - The actor's own account: the self-service follow-up
          (``finalize_password_change``).
        - Another account: their tokens are revoked and, if a usable password
          was set, the owner must choose a new one
          (``require_password_change_after_admin_set``). Their sessions end
          because the session auth hash derives from the password hash;
          Django refreshes only the actor's own session.
        """
        self._refuse_privileged_password_target(request, id)
        if request.method != "POST":
            return super().user_change_password(request, id, form_url)
        target = self.get_object(request, unquote(id))
        if target is None:
            # Django's view raises the 404 (or 403) itself.
            return super().user_change_password(request, id, form_url)
        with transaction.atomic():
            before = _stored_password_hash(target.pk, lock=True)
            response = super().user_change_password(request, id, form_url)
            if _stored_password_hash(target.pk) != before:
                target.refresh_from_db()
                if request.user.pk == target.pk:
                    finalize_password_change(target)
                else:
                    require_password_change_after_admin_set(target)
        return response

    def get_readonly_fields(self, request, obj=None):
        """Only a superuser may edit privilege-bearing fields (#1579)."""
        readonly = tuple(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += tuple(f for f in _SUPERUSER_ONLY_FIELDS if f not in readonly)
            if _is_protected_other_account(request.user, obj):
                # Same predicate as the password form: identity fields feed
                # password reset and ``is_active`` locks the account out, so
                # for these accounts they stay with superusers.
                readonly += tuple(
                    f for f in ("email", "username", "first_name", "last_name", "is_active")
                    if f not in readonly
                )
        return readonly

    def save_model(self, request, obj, form, change):
        """A user added here with a password must choose their own on first use.

        An account added with password sign-in disabled is left alone: the flag
        would make its owner create a password the administrator chose not to
        give it.
        """
        if not change and obj.has_usable_password():
            obj.must_change_password = True
        if not change:
            super().save_model(request, obj, form, change)
            return
        # Deactivating here applies the same revocation
        # housekeeping as the admin API (#1563). The stored values are read
        # under a row lock before the save because the ModelForm has already
        # written the submitted values onto ``obj``.
        from .admin_views import (
            access_state,
            apply_access_loss_revocations,
            deactivation_refusal,
            demotion_refusal,
        )

        with transaction.atomic():
            # One pk-ordered lock covers the target and every row the guards
            # count (active site admins and active superusers). A single
            # ordered statement gives concurrent saves the same lock order, so
            # they serialize instead of deadlocking, and two accounts removing
            # each other's access cannot both pass the last-admin checks.
            locked = list(
                User.objects.select_for_update()
                .filter(
                    Q(pk=obj.pk)
                    | Q(is_site_admin=True, is_active=True)
                    | Q(is_superuser=True, is_active=True)
                )
                .order_by("pk")
            )
            stored = next((u for u in locked if u.pk == obj.pk), None)
            submitted = (
                form.submitted_row_version()
                if isinstance(form, VisibanUserChangeForm) else None
            )
            if stored is not None and submitted is not None:
                # clean() compared row_version before this lock; compare it
                # again with the locked row so a submission made against an
                # older state of the account is refused (#1622). The groups
                # and permissions read here are checked again in
                # save_related, before they are written.
                fields = form._versioned_fields()
                if not row_version_matches(submitted, user_row_version(stored, fields)):
                    raise StaleSubmission
                form._locked_m2m_state = _m2m_state(stored, fields)
            if stored is not None and form is not None:
                # The form wrote its values onto the instance Django loaded
                # earlier in this request. Between that load and the lock
                # taken above, another request can change a field the form
                # does not carry (a demotion, a pending password-change
                # flag); those fields must come from the locked row, or this
                # save would write the older values back.
                #
                # The same holds for a field the form carries but the
                # administrator did not change (#1619): the form re-submits
                # every value it rendered, including the read-only password
                # hash, so only the fields in ``changed_data`` take the
                # submitted value. An unbound form (no submission) has no
                # ``changed_data`` to go by, so only the first rule applies.
                changed = set(form.changed_data) if getattr(form, "is_bound", False) else None
                for field in User._meta.concrete_fields:
                    if field.name not in form.fields or (
                        changed is not None and field.name not in changed
                    ):
                        setattr(obj, field.attname, getattr(stored, field.attname))
            if stored is not None:
                # The form's checks ran before this lock was taken; repeat them
                # against the locked rows. They run after the values above are
                # taken from the locked row, so they judge what this save will
                # write: a stale rendered value the administrator left alone is
                # not a deactivation or demotion.
                if (stored.is_active and not obj.is_active and deactivation_refusal(
                    stored, request.user, include_last_site_admin=True
                )) or (stored.is_superuser and not obj.is_superuser and demotion_refusal(
                    stored, request.user
                )):
                    raise PermissionDenied
            super().save_model(request, obj, form, change)
            if stored is not None:
                apply_access_loss_revocations(obj, request.user, access_state(stored))

    def save_related(self, request, form, formsets, change):
        """Write only the many-to-many fields the administrator changed (#1622).

        As with the concrete fields in ``save_model``, a many-to-many field
        (``groups``, ``user_permissions``) the administrator left alone keeps
        its stored value. Django's change view runs in one transaction, so the
        row lock ``save_model`` took is still held here.

        When a ``row_version`` was submitted, the groups and permissions are
        read again first; if they differ from what ``save_model`` read under
        the lock, the save is refused and rolled back.
        """
        locked_state = getattr(form, "_locked_m2m_state", None)
        if change and locked_state is not None:
            current = _m2m_state(User.objects.get(pk=form.instance.pk), list(locked_state))
            if current != locked_state:
                raise StaleSubmission
        if change and getattr(form, "is_bound", False):
            changed = set(form.changed_data)
            for field in User._meta.many_to_many:
                if field.name in form.fields and field.name not in changed:
                    form.cleaned_data.pop(field.name, None)
        super().save_related(request, form, formsets, change)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            _func, name, description = actions["delete_selected"]
            actions["delete_selected"] = (guarded_delete_selected, name, description)
        return actions

    def delete_view(self, request, object_id, extra_context=None):
        """Refuse a guarded delete with a message, on the confirm page and on submit (#1616)."""
        obj = self.get_object(request, unquote(object_id))
        if obj is not None and self.has_delete_permission(request, obj):
            reasons = user_deletion_refusals([obj], request.user)
            if reasons:
                return self._deletion_refused(request, reasons)
        try:
            return super().delete_view(request, object_id, extra_context)
        except DeletionRefused as exc:
            return self._deletion_refused(request, exc.messages)

    def _deletion_refused(self, request, reasons):
        for reason in reasons:
            self.message_user(request, reason, messages.ERROR)
        return HttpResponseRedirect(reverse("admin:accounts_user_changelist"))

    @staticmethod
    def _lock_and_check_admins(request, pks):
        """Statement 1: lock the targets, the actor and the active administrators.

        One pk-ordered ``FOR NO KEY UPDATE`` over the targets, the actor and
        every active site admin and superuser (the order ``save_model`` uses).
        Row flags (``is_active``, ``is_site_admin``, ``is_superuser``) change
        only through an UPDATE, which conflicts with this lock, and a second
        delete conflicts with it too, so the last-administrator, actor and
        actor-scope checks made here judge state that cannot move until this
        transaction ends. ``FOR NO KEY UPDATE`` is deliberately not
        ``FOR UPDATE``: it does not conflict with the ``FOR KEY SHARE`` that a
        foreign key to the user takes (a card write recording its author, a
        board insert naming its owner). That keeps a writer that already holds
        a card row from being blocked on the user row while this delete waits
        for that card (see ``_lock_cards_then_targets``). It also means this
        statement does *not* protect the owned-boards rule; statement 3 does.
        """
        actor = request.user
        locked = list(
            User.objects.select_for_update(no_key=True)
            .filter(
                Q(pk__in=pks)
                | Q(pk=actor.pk)
                | Q(is_site_admin=True, is_active=True)
                | Q(is_superuser=True, is_active=True)
            )
            .order_by("pk")
        )
        actor_row = next((u for u in locked if u.pk == actor.pk), None)
        if actor_row is None or not actor_row.is_active:
            raise PermissionDenied
        pk_set = set(pks)
        targets = [u for u in locked if u.pk in pk_set]
        if not actor_row.is_superuser and any(
            _is_protected_other_account(actor_row, t) for t in targets
        ):
            # Judged on the locked rows: a target promoted to staff or
            # superuser, or an actor demoted, since the request started.
            raise DeletionRefused(["Only a superuser can delete this account."])
        reasons = user_deletion_refusals(targets, actor)
        if reasons:
            raise DeletionRefused(reasons)
        return targets

    @staticmethod
    def _cards_to_lock(pks):
        """Cards the delete will write: the owned boards' cards and every card
        that carries a reference to a target.

        The user delete sets those references to NULL (``Card.assignee`` and
        ``created_by``, and the author, actor, uploader or creator on a card's
        movements, comments, activity, checklists, attachments, relations and
        notifications), which updates the card's rows. Locking the cards
        first keeps the order card, then user, the order a card writer uses.
        """
        from boards.models import (
            CardActivity,
            CardAttachment,
            CardChecklist,
            CardComment,
            CardMovement,
            CardRelation,
            Notification,
        )

        referenced = (
            Q(created_by__in=pks)
            | Q(assignee__in=pks)
            | Q(pk__in=CardMovement.objects.filter(moved_by__in=pks).values("card_id"))
            | Q(pk__in=CardComment.objects.filter(author__in=pks).values("card_id"))
            | Q(pk__in=CardActivity.objects.filter(actor__in=pks).values("card_id"))
            | Q(pk__in=CardChecklist.objects.filter(created_by__in=pks).values("card_id"))
            | Q(pk__in=CardAttachment.objects.filter(uploaded_by__in=pks).values("card_id"))
            | Q(pk__in=CardRelation.objects.filter(created_by__in=pks).values("from_card_id"))
            | Q(pk__in=CardRelation.objects.filter(created_by__in=pks).values("to_card_id"))
            | Q(
                pk__in=Notification.objects.filter(Q(actor__in=pks) | Q(recipient__in=pks))
                .exclude(card__isnull=True)
                .values("card_id")
            )
        )
        return Card.objects.filter(Q(board__owner__in=pks) | referenced)

    def _lock_cards_then_targets(self, request, pks):
        """Statements 2 and 3, in card, then user order.

        2. The owned boards' rows (``FOR NO KEY UPDATE``), then the cards from
           ``_cards_to_lock`` (one pk-ordered ``FOR UPDATE``), then the owned
           boards' entry columns, all in ``lock_cards_for_cascade``: the
           board -> card -> column order every container delete takes (#1602),
           so this delete and a REST column, swimlane or board delete queue
           rather than invert.
        3. The target user rows, one pk-ordered ``FOR UPDATE``, then the
           refusals are judged again, so the owned-boards rule is checked on
           boards committed up to this point.

        What this guarantees: the guard decisions (self, last site admin,
        last superuser, actor standing and scope) are judged on rows locked
        by statement 1, and a board is never silently deleted by a delete
        that did not check it. A board insert that has already reached
        COMMIT before statement 3 is seen by the re-check, so the delete is
        refused and the board kept. The foreign key from a board to its owner
        is deferred on PostgreSQL, so an insert still in flight takes its
        ``FOR KEY SHARE`` on the owner only at COMMIT: it waits for this
        transaction and then fails its foreign key because the owner is gone,
        and the board is not created.

        Lock order and its limit: a card writer holds a card row (``FOR NO
        KEY UPDATE``) and needs ``FOR KEY SHARE`` on a user row, at COMMIT
        for a deferred foreign key. Statement 1 is ``FOR NO KEY UPDATE``,
        which does not block that ``KEY SHARE``, and statement 2 takes the
        cards that carry a reference to a target before statement 3 takes the
        target rows, so for the cards snapshotted by statement 2 the order is
        card, then user, as a card writer's is. The snapshot is the limit: a
        card that gains a reference to a target after statement 2 and before
        statement 3 (it is assigned to the target, say) is not locked, and
        the cascade's ``SET NULL`` update of it then runs after this
        transaction already holds the target row ``FOR UPDATE``, while a
        writer that holds that card needs ``KEY SHARE`` on the target.
        PostgreSQL reports ``deadlock detected`` and aborts one side, which
        can be this delete (a server error); nothing is lost and the delete
        can be retried. Rows of tables that are not scoped to a card (invite
        links, board export log, group rows and so on) are not locked here
        and are not covered by this argument.
        """
        pk_list = list(pks)
        lock_cards_for_cascade(
            self._cards_to_lock(pk_list),
            entry_columns=Column.objects.filter(board__owner__in=pk_list),
            boards=Board.objects.filter(owner__in=pk_list),
        )
        locked = list(
            User.objects.select_for_update().filter(pk__in=pk_list).order_by("pk")
        )
        reasons = user_deletion_refusals(locked, request.user)
        if reasons:
            raise DeletionRefused(reasons)

    def delete_model(self, request, obj):
        # Board.owner is CASCADE, so deleting a user deletes their boards and
        # those boards' cards: lock the cards first, then those boards'
        # columns so no card enters them mid-cascade, in one transaction held
        # across the delete (#1603, #1602; see lock_cards_for_cascade).
        with transaction.atomic():
            self._lock_and_check_admins(request, [obj.pk])
            self._lock_cards_then_targets(request, [obj.pk])
            super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # A bulk action is not wrapped in a transaction by Django: one outer
        # atomic so the locks outlive the delete (#1603).
        with transaction.atomic():
            pks = list(queryset.values_list("pk", flat=True))
            self._lock_and_check_admins(request, pks)
            self._lock_cards_then_targets(request, pks)
            super().delete_queryset(request, queryset)


def _stored_password_hash(pk, *, lock=False):
    qs = User.objects.filter(pk=pk)
    if lock:
        qs = qs.select_for_update()
    return qs.values_list("password", flat=True).first()


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    """Singleton admin — clicking 'Site settings' goes straight to the edit form."""

    def has_add_permission(self, request):
        # Prevent creating additional rows; there is always exactly one.
        return not SiteSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        """Audit-log setting changes made through Django admin (#1126).

        Django admin is a genuine operator path for these toggles — it is the
        documented second break-glass route for maintenance mode — and it
        bypasses the REST serializer entirely, so without this override a flip
        made here would leave no entry in the action log.

        The previous state is re-read from the database rather than taken from
        ``obj``: by the time ``save_model`` runs, the ModelForm has already
        written the submitted values onto the instance, so ``obj`` holds the new
        state on both sides of the comparison.

        That re-read is taken under ``select_for_update`` inside the same
        transaction as the save, for the same reason as
        ``AdminSettingsView.patch``: an unlocked read lets a concurrent edit
        land between the snapshot and the save, which yields an audit row
        describing a transition that never happened.
        """
        with transaction.atomic():
            previous = SiteSetting.objects.select_for_update().filter(pk=obj.pk).first()
            # On the add path there is no stored row yet (obj.pk is None, and
            # `pk__exact=None` matches nothing). Diff against the model defaults
            # rather than skipping the audit: that form is reachable on a
            # first-boot instance, and an operator who creates the singleton
            # with maintenance mode already on should not be the one change that
            # goes unrecorded.
            before = snapshot_site_setting(previous if previous else SiteSetting())

            super().save_model(request, obj, form, change)
            record_site_setting_changes(
                before=before,
                after=obj,
                actor=request.user,
                source=AdminActionLog.Source.DJANGO_ADMIN,
            )

    def changelist_view(self, request, extra_context=None):
        # Skip the list view and redirect directly to the single object.
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        obj = SiteSetting.get()
        return HttpResponseRedirect(
            reverse("admin:accounts_sitesetting_change", args=[obj.pk])
        )


# Django's stock Group admin edits permission sets directly, so it is limited to
# active superusers (#1617).
admin.site.unregister(AuthGroup)


@admin.register(AuthGroup)
class SuperuserOnlyGroupAdmin(GroupAdmin):
    """Django's ``GroupAdmin`` (keeps its permission widgets), superuser-only."""

    @staticmethod
    def _superuser_only(request):
        return bool(request.user.is_active and request.user.is_superuser)

    def has_module_permission(self, request):
        return self._superuser_only(request)

    def has_view_permission(self, request, obj=None):
        return self._superuser_only(request)

    def has_add_permission(self, request):
        return self._superuser_only(request)

    def has_change_permission(self, request, obj=None):
        return self._superuser_only(request)

    def has_delete_permission(self, request, obj=None):
        return self._superuser_only(request)


# ---------------------------------------------------------------------------
# Third-party admins that carry a user's email, login identity or API token,
# plus the Sites admin (#1618, #1621)
#
# These admins hold sign-in identities, credentials or site-wide configuration,
# so access is limited to superusers regardless of model permissions. They stay
# registered for break-glass use.
#
# `allauth`, `django.contrib.sites` and `rest_framework.authtoken` precede
# `accounts` in INSTALLED_APPS, so their admin modules have already registered by the time
# this module is imported. If that ordering changes, or an app is removed,
# startup fails loudly instead of leaving a stock admin unrestricted.
# ---------------------------------------------------------------------------
class SuperuserOnlyAdminMixin:
    """Limit every admin permission hook to active superusers."""

    @staticmethod
    def _is_active_superuser(request):
        return bool(request.user.is_active and request.user.is_superuser)

    def has_module_permission(self, request):
        return self._is_active_superuser(request)

    def has_view_permission(self, request, obj=None):
        return self._is_active_superuser(request)

    def has_add_permission(self, request):
        return self._is_active_superuser(request)

    def has_change_permission(self, request, obj=None):
        return self._is_active_superuser(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_active_superuser(request)


def _restrict_to_superusers(model):
    """Re-register ``model``'s existing admin with the superuser-only mixin."""
    original = type(admin.site._registry[model])
    admin.site.unregister(model)
    restricted = type(
        original.__name__, (SuperuserOnlyAdminMixin, original), {"__module__": __name__}
    )
    admin.site.register(model, restricted)


def _restrict_third_party_admins():
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
    from django.contrib.sites.models import Site
    from rest_framework.authtoken.models import TokenProxy

    for model in (EmailAddress, SocialAccount, SocialToken, SocialApp, TokenProxy, Site):
        _restrict_to_superusers(model)


_restrict_third_party_admins()
