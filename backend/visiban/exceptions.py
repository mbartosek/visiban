"""Shared DRF exceptions.

``CodedValidationError`` (#1660)
-------------------------------
A 400 whose body is exactly ``{"code": "<slug>", "detail": "<message>"}`` with
both values plain strings — the shape 1.1 shipped for its machine-checkable
errors (``feature_disabled``, ``export_restricted``), which clients compare with
``body.code === "..."``.

Raising ``serializers.ValidationError({"code": ..., "detail": ...})`` from a
serializer's ``validate()`` does **not** produce that shape: DRF's
``Serializer.run_validation`` catches every ``ValidationError`` and re-wraps it
through ``as_serializer_error()``, which coerces each dict value into a
single-item list (``{"code": ["relation_exists"], ...}``). A client checking
``body.code === "relation_exists"`` then silently never matches.

This class deliberately subclasses ``APIException`` and **not**
``ValidationError``, so ``run_validation`` does not catch it: it propagates out
of ``is_valid()`` untouched and DRF's default exception handler renders
``.detail`` as-is. The consequence callers must know about: it escapes
``is_valid()`` even without ``raise_exception=True``, so it is only for
serializers whose callers let DRF render the error (a view), not for code that
inspects ``serializer.errors``.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class CodedValidationError(APIException):
    """A 400 with a string ``code`` and string ``detail`` — the 1.1 shape."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Invalid input."
    default_code = "invalid"

    def __init__(self, code: str, detail: str):
        # Passing a dict makes APIException keep it as the body verbatim
        # (values become ErrorDetail, a str subclass, so they render as JSON
        # strings), rather than wrapping a bare string under a `detail` key.
        super().__init__(detail={"code": code, "detail": detail}, code=code)
