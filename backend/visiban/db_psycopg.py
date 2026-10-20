"""Backport of Django's psycopg 3 optimized ``timestamptz`` loader (#1800).

Why this module exists
----------------------
Django 5.2 adapts every ``timestamptz`` column through
``django.db.backends.postgresql.psycopg_any.BaseTzLoader``. That class subclasses
psycopg's *pure-Python* ``psycopg.types.datetime.TimestamptzLoader``, so every
datetime in every result row is parsed by a regex in Python. This happens even with
``psycopg[binary]`` installed, whose C loader is never reached (Django ticket
#36960). psycopg2 parsed these values in C.

When #1252 moved Visiban from psycopg2 to psycopg 3 (merged 2026-10-04),
``GET /boards/{id}/full/`` got about 12% slower at the mean and about 25% slower at
p95 on the nightly load test's ~2,400-card board: the response loads ~44,000
``timestamptz`` values. The query count and SQL did not change, so only the
latency job could see it. See ``docs/development/nightly-load-test.md``.

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

Remove this module and its call in ``boards.apps`` once the ``Django`` pin
reaches 6.1. :func:`apply` already turns itself into a no-op on a Django whose
``BaseTzLoader`` no longer subclasses the Python loader, and
``boards/tests/test_db_psycopg.py`` asserts that.
"""

from __future__ import annotations

_APPLIED_ATTR = "_visiban_1800_fast_tz_loader"


def apply() -> bool:
    """Patch Django's ``BaseTzLoader`` to delegate to psycopg's default loader.

    Returns True if the patch is (now) in place and False if it does not apply:
    psycopg 3 is not installed, or the running Django already has the upstream
    fix. Safe to call more than once.
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

    base.__init__ = __init__
    base.load = load
    setattr(base, _APPLIED_ATTR, True)
    return True
