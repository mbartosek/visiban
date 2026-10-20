"""Tests for the psycopg 3 ``timestamptz`` loader backport (#1800).

Background: ``visiban/db_psycopg.py``. Django 5.2 parses every ``timestamptz``
value with psycopg's pure-Python loader. After the psycopg2 to psycopg 3 move
(#1252) that cost ``GET /boards/{id}/full/`` ~12% at the mean on the nightly
load-test board, with the query count and SQL unchanged. A query-count test cannot
see that regression, so these tests pin the mechanism instead:

* the patched loader returns exactly what Django's own loader returned,
* with the C loader available, the Python ISO parser is never called on a real
  query, and
* the patch steps aside on a Django that already carries the upstream fix.
"""

import contextlib
import datetime
from unittest import mock, skipUnless

from django.db import connection
from django.db.backends.postgresql import psycopg_any
from django.test import SimpleTestCase, TestCase

from visiban import db_psycopg

if psycopg_any.is_psycopg3:
    from psycopg import adapters, pq
    from psycopg.postgres import types as pg_types
    from psycopg.types.datetime import TimestamptzLoader as PyTimestamptzLoader

    TIMESTAMPTZ_OID = pg_types["timestamptz"].oid
    HAS_C_LOADER = pq.__impl__ in ("c", "binary")

# Text-format timestamptz values as PostgreSQL sends them: with and without
# micros, a short fraction, non-UTC and fractional-hour offsets, an offset with
# seconds (pre-1900 LMT zones), and the year boundaries.
SAMPLES = [
    b"2026-10-04 10:31:33.123456+00",
    b"2026-10-04 10:31:33+00",
    b"2026-10-04 10:31:33.5+00",
    b"2026-10-04 10:31:33.000001-04",
    b"2026-03-08 02:30:00+05:30",
    b"1883-11-18 12:03:58-04:56:02",
    b"0001-01-01 00:00:00+00",
    b"9999-12-31 23:59:59.999999+00",
]


@skipUnless(psycopg_any.is_psycopg3, "the backport only applies to psycopg 3")
class TimestamptzLoaderBackportTests(SimpleTestCase):
    def test_apply_is_in_effect_after_app_loading(self):
        # boards.apps.BoardsConfig.ready() calls apply(); a second call is a no-op.
        self.assertTrue(getattr(psycopg_any.BaseTzLoader, db_psycopg._APPLIED_ATTR, False))
        load_before = psycopg_any.BaseTzLoader.load
        self.assertTrue(db_psycopg.apply())
        self.assertIs(psycopg_any.BaseTzLoader.load, load_before)

    def test_delegates_to_psycopgs_default_loader(self):
        loader = psycopg_any.BaseTzLoader(TIMESTAMPTZ_OID, None)
        default_cls = adapters.get_loader(TIMESTAMPTZ_OID, pq.Format.TEXT)
        self.assertIs(type(loader._visiban_default_loader), default_cls)
        if HAS_C_LOADER:
            # The whole point: the default with psycopg[binary] is the C class,
            # not the Python one Django 5.2 subclasses.
            self.assertIsNot(default_cls, PyTimestamptzLoader)

    def test_values_match_djangos_original_loader(self):
        # Original Django 5.2 behavior: Python parse, then replace(tzinfo=tz).
        # Covers USE_TZ=True (UTC) and USE_TZ=False (None strips tzinfo).
        for tz in (datetime.timezone.utc, None):
            with self.subTest(tz=tz):
                patched_cls = type("Patched", (psycopg_any.BaseTzLoader,), {"timezone": tz})
                patched = patched_cls(TIMESTAMPTZ_OID, None)
                original = PyTimestamptzLoader(TIMESTAMPTZ_OID, None)
                for raw in SAMPLES:
                    expected = original.load(raw).replace(tzinfo=tz)
                    got = patched.load(raw)
                    self.assertEqual(got, expected, raw)
                    self.assertEqual(got.tzinfo, expected.tzinfo, raw)
                    self.assertEqual(got.isoformat(), expected.isoformat(), raw)

    def test_noop_when_django_already_has_the_upstream_fix(self):
        # Django >= 6.1 (commit 994fa77) makes BaseTzLoader an adapt.Loader that
        # wraps the default loader itself. apply() must leave such a class alone.
        from psycopg import adapt

        class UpstreamBaseTzLoader(adapt.Loader):
            timezone = None

            def load(self, data):  # pragma: no cover - never called
                raise AssertionError

        with mock.patch.object(psycopg_any, "BaseTzLoader", UpstreamBaseTzLoader):
            self.assertFalse(db_psycopg.apply())
        self.assertFalse(hasattr(UpstreamBaseTzLoader, db_psycopg._APPLIED_ATTR))
        self.assertNotIn("_visiban_default_loader", UpstreamBaseTzLoader.__dict__)


@skipUnless(
    connection.vendor == "postgresql" and psycopg_any.is_psycopg3,
    "the timestamptz loader only runs on PostgreSQL with psycopg 3",
)
class TimestamptzLoaderOnRealQueriesTests(TestCase):
    def test_query_results_are_aware_utc_and_skip_the_python_parser(self):
        from accounts.models import User

        user = User.objects.create_user(username="tzloader1800", password=None)
        fail_if_called = mock.patch.object(
            PyTimestamptzLoader,
            "_load_iso",
            side_effect=AssertionError("pure-Python timestamptz parser used (#1800)"),
        )
        with fail_if_called if HAS_C_LOADER else contextlib.nullcontext():
            joined = User.objects.values_list("date_joined", flat=True).get(pk=user.pk)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT %s::timestamptz",
                    ["2026-10-04 10:31:33.123456-04:30"],
                )
                (raw_value,) = cursor.fetchone()

        self.assertEqual(joined, user.date_joined)
        self.assertIs(joined.tzinfo, datetime.timezone.utc)
        self.assertEqual(
            raw_value,
            datetime.datetime(2026, 10, 4, 15, 1, 33, 123456, tzinfo=datetime.timezone.utc),
        )
        self.assertIs(raw_value.tzinfo, datetime.timezone.utc)
