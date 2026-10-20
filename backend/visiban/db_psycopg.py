"""Backport of Django's psycopg 3 optimized ``timestamptz`` loader (#1800).

Why this module exists
----------------------
Django 5.2 adapts every ``timestamptz`` column through
``django.db.backends.postgresql.psycopg_any.BaseTzLoader``. That class subclasses
psycopg's *pure-Python* ``psycopg.types.datetime.TimestamptzLoader``, so every
datetime in every result row is parsed by a regex in Python. This happens even with
``psycopg[binary]`` installed, whose C loader is never reached (Django ticket
#36960). psycopg2 parsed these values in C.

#1252 moved Visiban from psycopg2 to psycopg 3 (merged 2026-10-04). Measured
locally on the nightly load test's ~2,400-card fixture, that switch alone made
``GET /boards/{id}/full/`` about 12% slower at the mean, with the same queries and
byte-identical responses. The response loads ~44,000 ``timestamptz`` values. The
Python parser is the largest identified part of the step: about 45ms of 74ms. This
backport mitigates that part, removing about 36ms per request, which is roughly a
third to a half of the step. #1807 attributed the rest: mostly the per-value
``.replace(tzinfo=...)`` this backport keeps (as Django 6.1 does), psycopg 3's
client-side binding of large prefetch ``IN`` lists in Python, and the extra
garbage collection that binding causes.
The query count and SQL did not change, so only a latency measurement can see this.
See ``docs/development/nightly-load-test.md``.

Django fixed this upstream in 6.1 (commit 994fa77). The fix stops inheriting from
the Python loader. It instead wraps whatever loader psycopg registered by default
(the C one when the binary or C package is installed) and only re-applies the
connection timezone. Visiban pins ``Django<6``, so this module applies the same
change to the 5.2 class in place:

* ``__init__`` builds the default loader, using the public
  ``psycopg.adapters.get_loader()`` API the upstream fix uses.
* ``load`` delegates to that loader, then ``.replace(tzinfo=self.timezone)``.
  This is the same post-processing the original method does, so the returned
  values are identical.

The methods are patched on ``BaseTzLoader`` itself rather than by registering a
new class. ``register_tzloader()`` subclasses ``BaseTzLoader`` both at the time a
connection template is built (``get_adapters_template`` is ``lru_cache``-d) and
for each cursor, so patching the base class covers both without touching Django's
connection code.

Before patching, :func:`apply` checks that ``BaseTzLoader`` is still the class this
backport was written against: its source must hash to
:data:`EXPECTED_BASE_TZ_LOADER_SHA256`, which is the same in Django 5.2.15 through
5.2.17 and on ``stable/5.2.x``. ``requirements.txt`` accepts any 5.2.x from 5.2.15
on, so a later patch release could change the class. In that case, or when the
source cannot be read, the patch is skipped with a logged warning and Django's own
loader stays in place. ``boards/tests/test_db_psycopg.py`` fails on that state, so
CI flags a Django bump that changed the class.

Remove this module and its call in ``boards.apps`` once the ``Django`` pin
reaches 6.1. :func:`apply` already turns itself into a no-op on a Django whose
``BaseTzLoader`` no longer subclasses the Python loader, and
``boards/tests/test_db_psycopg.py`` asserts that.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import textwrap

logger = logging.getLogger(__name__)

_APPLIED_ATTR = "_visiban_1800_fast_tz_loader"

# sha256 of textwrap.dedent(inspect.getsource(BaseTzLoader)) for the class this
# backport replaces: Django 5.2.15, 5.2.17 and stable/5.2.x all match.
EXPECTED_BASE_TZ_LOADER_SHA256 = "4c1a54456d8cfdd2c0a42f3d5af6ff821e1758054ebf73e36a50e6ad0d4f1cc5"

# Django's own BaseTzLoader methods, captured just before they are replaced, so
# tests can compare the backport against the real original rather than a copy.
ORIGINAL_METHODS: dict = {}


def source_fingerprint(cls) -> str | None:
    """sha256 of ``cls``'s dedented source, or None when it cannot be read."""
    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):
        return None
    return hashlib.sha256(textwrap.dedent(source).encode("utf-8")).hexdigest()


def apply() -> bool:
    """Patch Django's ``BaseTzLoader`` to delegate to psycopg's default loader.

    Returns True if the patch is (now) in place. Returns False if it does not
    apply: psycopg 3 is not installed, the running Django already has the
    upstream fix, or ``BaseTzLoader`` is not the class this backport was written
    for (logged as a warning). Safe to call more than once.
    """
    try:
        from django.db.backends.postgresql import psycopg_any
    except ImportError:  # pragma: no cover - neither psycopg nor psycopg2 installed
        return False
    if not psycopg_any.is_psycopg3:
        return False

    from psycopg import adapters, pq
    from psycopg.postgres import types
    from psycopg.types.datetime import TimestamptzLoader as PyTimestamptzLoader

    base = psycopg_any.BaseTzLoader
    if getattr(base, _APPLIED_ATTR, False):
        return True
    if not issubclass(base, PyTimestamptzLoader):
        # Django >= 6.1 already wraps the default loader; nothing to backport.
        return False
    fingerprint = source_fingerprint(base)
    if fingerprint != EXPECTED_BASE_TZ_LOADER_SHA256:
        # Patching a class we have not reviewed could change datetime values, so
        # keep Django's slower but correct loader instead.
        logger.warning(
            "Not applying the psycopg timestamptz loader backport (#1800): "
            "Django's BaseTzLoader source fingerprint is %s, expected %s. "
            "Review visiban/db_psycopg.py against the installed Django.",
            fingerprint,
            EXPECTED_BASE_TZ_LOADER_SHA256,
        )
        return False

    # The loader psycopg registers by default: the C class with psycopg[binary]
    # or psycopg[c], the Python class otherwise. Delegating to the Python class
    # is still correct, just not faster.
    default_loader_cls = adapters.get_loader(types["timestamptz"].oid, pq.Format.TEXT)

    def __init__(self, oid, context=None):
        # Keep the Python parent's __init__ so the instance stays a fully formed
        # TimestamptzLoader for anything that inspects it.
        PyTimestamptzLoader.__init__(self, oid, context)
        self._visiban_default_loader = default_loader_cls(oid, context)

    def load(self, data):
        return self._visiban_default_loader.load(data).replace(tzinfo=self.timezone)

    ORIGINAL_METHODS["__init__"] = base.__init__
    ORIGINAL_METHODS["load"] = base.load
    base.__init__ = __init__
    base.load = load
    setattr(base, _APPLIED_ATTR, True)
    return True
