"""django-filter backend whose 400 carries the bad-query-param ``code`` (#1832).

A filter value that fails its type check (``?assignee=x``, ``?due_before=x``,
``?updated_since=x``) makes ``DjangoFilterBackend`` raise DRF's field-keyed
``{"assignee": ["Enter a number."]}``. #1825 settled ``{detail, code}`` with
``code: "invalid_query_param"`` as the API's shape for a bad query parameter.

The field-keyed entry is kept and ``detail``/``code`` are added beside it,
rather than replacing it. ``GET /boards/{id}/cards/`` shipped that field-keyed
body in 1.1, and removing a response field is a breaking change in a minor
release (CLAUDE.md, "Backward compatibility"). ``GET /cards/`` is new in 1.2
and could have taken the bare shape, but it mirrors the board card list's
filters on purpose; one backend gives both the same body for the same
parameter rather than forking the error shape per endpoint.

Wired as the first ``DEFAULT_FILTER_BACKENDS`` entry and on views that set
``filter_backends`` explicitly. A FilterSet must therefore never declare a
filter named ``detail`` or ``code``: its field-keyed entry would collide with
the added keys.
"""

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import ValidationError

from visiban.exceptions import INVALID_QUERY_PARAM


def _summarize(errors):
    """``{"assignee": ["Enter a number."]}`` -> ``"assignee: Enter a number."``."""
    parts = []
    for field, messages in errors.items():
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        parts.append(f"{field}: {' '.join(str(m) for m in messages)}")
    return "; ".join(parts)


class CodedDjangoFilterBackend(DjangoFilterBackend):
    """``DjangoFilterBackend`` whose filter errors also carry ``detail``/``code``."""

    def filter_queryset(self, request, queryset, view):
        try:
            return super().filter_queryset(request, queryset, view)
        except ValidationError as exc:
            # translate_validation() always raises a dict; anything else is not
            # a filterset error and passes through untouched.
            if not isinstance(exc.detail, dict):
                raise
            body = dict(exc.detail)
            body["detail"] = _summarize(exc.detail)
            body["code"] = INVALID_QUERY_PARAM
            raise ValidationError(body) from None
