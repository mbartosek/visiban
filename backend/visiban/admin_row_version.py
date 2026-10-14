"""Rendered-state checks for Django admin change forms.

A Django admin change form re-submits every value it rendered. Two rules keep
a submission from writing back values that were current when the page was
rendered but have been changed since by someone else:

1. Only the fields the administrator changed (``form.changed_data``) are
   written; every other field, concrete or many-to-many, keeps the value
   stored when the save runs (``RowVersionAdminMixin.keep_unchanged_fields``
   and ``save_related``).
2. The change form renders a hidden ``row_version``, a keyed digest of the
   stored values of the fields the form writes. A submission made against an
   older state is refused: ``RowVersionFormMixin.clean`` compares it with the
   row read when the submission arrives (a form error asking for a reload),
   the admin's ``save_model`` compares it again with the row read under the
   row lock (``check_row_version``), and ``save_related`` re-reads the
   many-to-many values before writing them. A refusal under the lock raises
   ``StaleSubmission``, which rolls back Django's change-view transaction
   (and any ``transaction.on_commit`` broadcast queued in it) and is answered
   with a redirect to the change page and an error message.

A submission without ``row_version`` (one not produced by the rendered form)
skips both comparisons. It carries no privilege, since the same administrator
could submit any values, and rule 1 still applies to it.

The pieces are model-agnostic so every admin whose change form carries
privilege-bearing values can use them; ``VisibanUserAdmin`` was the first.
"""

import json

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.utils.crypto import constant_time_compare, salted_hmac

#: Hidden field carrying the stored state as the change form rendered it.
ROW_VERSION_FIELD = "row_version"

STALE_FORM_MESSAGE = (
    "This record was changed after this page was opened. Reload the page and "
    "make your changes again."
)

#: Shown after a redirect to the change page, which then shows the current state.
STALE_SAVE_MESSAGE = (
    "This record was changed after this page was opened, so nothing was saved. "
    "The page now shows the current state; make your changes again."
)


class StaleSubmission(Exception):
    """The submitted ``row_version`` does not match the row read under the lock."""


def default_row_version_salt(model):
    """``<app_label>.admin.<model_name>_row_version``: one digest key per model.

    For the user model this is ``accounts.admin.user_row_version``, the salt
    the user change form has always used, so its digests are unchanged.
    """
    return f"{model._meta.app_label}.admin.{model._meta.model_name}_row_version"


def m2m_state(instance, field_names):
    """Sorted primary keys of *instance*'s many-to-many fields among *field_names*."""
    opts = instance._meta
    state = {}
    for name in sorted(field_names):
        if opts.get_field(name).many_to_many:
            # Read from the database, not a prefetch cache.
            state[name] = sorted(getattr(instance, name).values_list("pk", flat=True))
    return state


def row_version(instance, field_names, salt):
    """A keyed digest of *instance*'s stored values for the form fields *field_names*.

    The digest is keyed with ``SECRET_KEY`` (``salted_hmac``) so the hidden
    value says nothing about the stored values.
    """
    opts = instance._meta
    state = {"pk": instance.pk, **m2m_state(instance, field_names)}
    for name in field_names:
        field = opts.get_field(name)
        if not field.many_to_many:
            state[name] = field.value_from_object(instance)
    payload = json.dumps(state, sort_keys=True, default=str)
    return salted_hmac(salt, payload).hexdigest()


def row_version_matches(submitted, current):
    return constant_time_compare(submitted, current)


class RowVersionField(forms.CharField):
    """Hidden ``row_version``; never reported as a changed field.

    It is not record data, so it stays out of ``changed_data`` and the admin
    history's change message.
    """

    widget = forms.HiddenInput

    def has_changed(self, initial, data):
        return False


class RowVersionFormMixin:
    """ModelForm mixin: render ``row_version`` and refuse a stale submission.

    The concrete form declares ``row_version = RowVersionField(required=False)``
    itself (Django collects declared fields only from form classes). Override
    ``row_version_fields`` to change which fields the digest covers, and set
    ``stale_form_message`` for a model-specific message.
    """

    stale_form_message = STALE_FORM_MESSAGE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stored_row_version = None
        if self.instance.pk and ROW_VERSION_FIELD in self.fields:
            self._stored_row_version = row_version(
                self.instance, self.row_version_fields(), self.row_version_salt()
            )
            self.fields[ROW_VERSION_FIELD].initial = self._stored_row_version

    @classmethod
    def row_version_salt(cls):
        return default_row_version_salt(cls._meta.model)

    def row_version_fields(self):
        """The model fields this form can write (disabled fields write nothing).

        Extension point: a subclass that writes further fields of the row, or
        writes them some other way, extends this list so they are covered.
        """
        model_fields = {f.name for f in self._meta.model._meta.get_fields() if f.concrete}
        return [
            name for name, field in self.fields.items()
            if name in model_fields and not field.disabled
        ]

    # The name the user change form and its tests use.
    def _versioned_fields(self):
        return self.row_version_fields()

    def submitted_row_version(self):
        """The submitted ``row_version``, or ``None`` when absent or unbound."""
        if not self.is_bound:
            return None
        return getattr(self, "cleaned_data", {}).get(ROW_VERSION_FIELD) or None

    def clean(self):
        cleaned = super().clean()
        submitted = cleaned.get(ROW_VERSION_FIELD)
        if (
            self.is_bound
            and submitted
            and self._stored_row_version is not None
            and not row_version_matches(submitted, self._stored_row_version)
        ):
            raise ValidationError(self.stale_form_message, code="stale_form")
        return cleaned


class RowVersionAdminMixin:
    """ModelAdmin mixin for a change form built on ``RowVersionFormMixin``.

    The admin's ``save_model`` locks the row, reads it, and calls
    ``check_row_version(form, stored)`` and usually
    ``keep_unchanged_fields(obj, form, stored)`` before saving. Django's
    change view runs ``save_model`` and ``save_related`` in one transaction,
    so the row lock is still held when ``save_related`` writes the
    many-to-many fields.
    """

    stale_save_message = STALE_SAVE_MESSAGE

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Turn a refusal under the row lock into a message, not a server error.

        ``StaleSubmission`` propagates out of Django's transaction around the
        change view, so everything the save wrote (and every broadcast queued
        with ``transaction.on_commit``) is rolled back; the administrator is
        sent back to the change page, which shows the current state.
        """
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except StaleSubmission:
            self.message_user(request, self.stale_save_message, messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())

    def get_fieldsets(self, request, obj=None):
        """The change form also carries the hidden ``row_version``.

        A view-only page has no form to submit, and Django renders every field
        there as read-only text, so the field is left out; so is the add page,
        which has no stored state.
        """
        fieldsets = super().get_fieldsets(request, obj)
        # A form that declares row_version lists it among its fields when the
        # admin has no explicit fieldsets; place it once, below, or not at all.
        fieldsets = [
            (name, {**options, "fields": tuple(
                f for f in options.get("fields", ()) if f != ROW_VERSION_FIELD
            )})
            for name, options in fieldsets
        ]
        if obj is None or not fieldsets or not self.has_change_permission(request, obj):
            return fieldsets
        (name, options), *rest = fieldsets
        return [(name, {**options, "fields": (*options["fields"], ROW_VERSION_FIELD)}), *rest]

    def check_row_version(self, form, stored):
        """Compare the submitted ``row_version`` with *stored*, read under the row lock.

        ``clean`` compared it before the lock; a change committed between that
        read and the lock is caught here. The many-to-many values read here
        are compared again in ``save_related`` before they are written.
        """
        if stored is None or not isinstance(form, RowVersionFormMixin):
            return
        submitted = form.submitted_row_version()
        if submitted is None:
            return
        fields = form.row_version_fields()
        if not row_version_matches(submitted, row_version(stored, fields, form.row_version_salt())):
            raise StaleSubmission
        form._locked_m2m_state = m2m_state(stored, fields)

    @staticmethod
    def keep_unchanged_fields(obj, form, stored):
        """Take every concrete field the administrator did not change from *stored*.

        The form wrote its values onto the instance Django loaded earlier in
        this request. A field the form does not carry, or carries but the
        administrator left alone, may have been changed by another request
        since; it comes from the locked row so this save does not write the
        older value back. An unbound form has no ``changed_data`` to go by, so
        only the first rule applies.
        """
        if stored is None or form is None:
            return
        changed = set(form.changed_data) if getattr(form, "is_bound", False) else None
        for field in type(obj)._meta.concrete_fields:
            if field.name not in form.fields or (
                changed is not None and field.name not in changed
            ):
                setattr(obj, field.attname, getattr(stored, field.attname))

    def save_related(self, request, form, formsets, change):
        """Write only the many-to-many fields the administrator changed.

        As with the concrete fields, a many-to-many field the administrator
        left alone keeps its stored value. When a ``row_version`` was
        submitted, the many-to-many values are read again first; if they
        differ from what ``save_model`` read under the lock, the save is
        refused and rolled back.
        """
        locked_state = getattr(form, "_locked_m2m_state", None)
        if change and locked_state:
            current = m2m_state(
                self.model._default_manager.get(pk=form.instance.pk), list(locked_state)
            )
            if current != locked_state:
                raise StaleSubmission
        if change and getattr(form, "is_bound", False):
            changed = set(form.changed_data)
            for field in self.model._meta.many_to_many:
                if field.name in form.fields and field.name not in changed:
                    form.cleaned_data.pop(field.name, None)
        super().save_related(request, form, formsets, change)
