"""Request-shape guards shared by views that read ``request.data`` directly.

DRF only validates body shape when a serializer is involved. A view that calls
``request.data.get(...)`` or feeds a body/URL value straight into an ORM
lookup turns a well-formed-but-wrong-shaped request (a JSON array or scalar
body, ``"abc"`` where an id belongs) into an unhandled 500 (#1136, #1669).
These helpers keep the rejection in one place and always a 400.
"""
from rest_framework.exceptions import ParseError, ValidationError


def require_json_object(request):
    """Return ``request.data`` if it is a JSON object, else raise a 400.

    Valid JSON that is not an object (array, number, string, null, bool) makes
    ``request.data.get`` raise AttributeError. ``ParseError`` renders as the
    ``{"detail": ...}`` 400 shape the neighboring endpoints already use.
    """
    data = request.data
    if not isinstance(data, dict):
        raise ParseError("Request body must be a JSON object.")
    return data


def coerce_int_id(value, field="id"):
    """Return ``value`` as an int, or raise a 400 naming ``field``.

    Accepts ints and digit strings (the forms clients already send); rejects
    bools, floats, lists, dicts and non-numeric strings so they never reach an
    ORM ``pk=`` lookup, where they would raise ValueError/TypeError (a 500).
    """
    if isinstance(value, bool):
        raise ValidationError({field: "A valid integer is required."})
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValidationError({field: "A valid integer is required."})
