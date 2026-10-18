"""Request-shape guards shared by views that read ``request.data`` directly.

DRF only validates body shape when a serializer is involved. A view that calls
``request.data.get(...)`` or feeds a body/URL value straight into an ORM
lookup turns a well-formed-but-wrong-shaped request (a JSON array or scalar
body, ``"abc"`` where an id belongs) into an unhandled 500 (#1136, #1669).
These helpers keep the rejection in one place and always a 400.
"""
from rest_framework.exceptions import ParseError, ValidationError


# Largest value a 64-bit signed integer column can hold; anything bigger makes
# the ORM raise OverflowError (SQLite) or DataError (PostgreSQL) -> a 500.
MAX_DB_INT = 2**63 - 1


# Offsets are added to a page size before slicing (``qs[o:o + n]``), so they get
# headroom below MAX_DB_INT; 2**62 is far beyond any real row count.
MAX_OFFSET = 2**62


def id_in_db_range(value):
    """True if ``value`` is a non-negative int a database id column can hold."""
    return 0 <= value <= MAX_DB_INT


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


def optional_text(body, field):
    """Return ``body[field]`` stripped, ``""`` when absent/blank, else raise a 400.

    For optional free-text fields read straight off ``request.data`` (outside
    a serializer). A truthy non-string would raise AttributeError on
    ``.strip()``; it is a field-keyed 400 instead. Falsy values keep their
    long-standing meaning of "not supplied".
    """
    value = body.get(field) or ""
    if not isinstance(value, str):
        raise ValidationError({field: "Must be a string."})
    return value.strip()


def coerce_int_id(value, field="id"):
    """Return ``value`` as an int, or raise a 400 naming ``field``.

    Accepts ints and digit strings (the forms clients already send) within the 64-bit
    range; rejects out-of-range values,
    bools, floats, lists, dicts and non-numeric strings so they never reach an
    ORM ``pk=`` lookup, where they would raise ValueError/TypeError (a 500).
    """
    if isinstance(value, bool):
        raise ValidationError({field: "A valid integer is required."})
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        parsed = None
    if parsed is not None and id_in_db_range(parsed):
        return parsed
    raise ValidationError({field: "A valid integer is required."})


def parse_offset(request, param="offset"):
    """Return the ``offset`` query param as a non-negative int, or raise a 400.

    Long-standing leniency is kept for compatibility: an absent, blank, or
    non-numeric value is 0 and a negative value clamps to 0 (a negative slice
    bound is an AssertionError -> 500 on a queryset). A value past ``MAX_OFFSET``
    cannot be a real row position and overflows in the database driver
    (#1696), so it is rejected with the same field-keyed 400 as #1669.
    """
    raw = request.query_params.get(param, 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > MAX_OFFSET:
        raise ValidationError({param: "Must be a valid integer offset."})
    return value
