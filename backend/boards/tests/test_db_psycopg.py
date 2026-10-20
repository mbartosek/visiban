"""Tests for the psycopg 3 ``timestamptz`` loader backport (#1800).

Background: ``visiban/db_psycopg.py``. Django 5.2 parses every ``timestamptz``
value with psycopg's pure-Python loader. After the psycopg2 to psycopg 3 move
(#1252) that cost ``GET /boards/{id}/full/`` ~12% at the mean on the nightly
load-test board, with the query count and SQL unchanged. A query-count test cannot
see that regression, so these tests pin the mechanism instead:

* the patched loader returns exactly what Django's own (captured, original)
  ``load`` returns, values, tzinfo and errors alike, for every session TimeZone
  and Django timezone combination tested,
* with the C loader available, the Python ISO parser is never called on a real
  query, and
* the patch steps aside on a Django that already carries the upstream fix, and on
  a ``BaseTzLoader`` whose source differs from the one it was written for.
"""

import contextlib
import datetime
import zoneinfo
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
    # Overflow fallback: shifting to the session zone runs past datetime.max or
    # before datetime.min, so the loader returns the value in a fixed offset.
    b"9999-12-31 23:59:59-01",
    b"0001-01-01 00:00:00+01",
    # Values Python's datetime cannot represent: both loaders must raise the
    # same DataError.
    b"infinity",
    b"-infinity",
    b"2000-01-01 00:00:00+00 BC",
    b"0044-03-15 12:00:00+00 BC",
]

# Django's ``timezone`` for the loader: UTC under USE_TZ=True, None under
# USE_TZ=False (tzinfo is chopped), and a named zone (a per-DATABASES TIME_ZONE).
DJANGO_TIMEZONES = [datetime.timezone.utc, None, zoneinfo.ZoneInfo("America/New_York")]


def _outcome(fn):
    """Comparable result of one load call: the value or the raised error."""
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - error parity is what is under test
        return ("error", type(exc), str(exc))
    return ("value", value, value.isoformat(), repr(value.tzinfo))


def assert_matches_original(testcase, context):
    """Compare the backport with Django's captured original ``load`` on one instance."""
    original_load = db_psycopg.ORIGINAL_METHODS["load"]
    for tz in DJANGO_TIMEZONES:
        loader_cls = type("SpecificTzLoader", (psycopg_any.BaseTzLoader,), {"timezone": tz})
        loader = loader_cls(TIMESTAMPTZ_OID, context)
        for raw in SAMPLES:
            with testcase.subTest(tz=tz, raw=raw):
                testcase.assertEqual(
                    _outcome(lambda: loader.load(raw)),
                    _outcome(lambda: original_load(loader, raw)),
                )


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

    def test_original_methods_were_captured_from_django(self):
        # The comparison below is only meaningful against Django's real method.
        original_load = db_psycopg.ORIGINAL_METHODS["load"]
        self.assertEqual(original_load.__module__, psycopg_any.__name__)
        self.assertEqual(original_load.__qualname__, "BaseTzLoader.load")
        self.assertIs(db_psycopg.ORIGINAL_METHODS["__init__"], PyTimestamptzLoader.__init__)

    def test_values_and_errors_match_djangos_original_loader_without_a_connection(self):
        # context=None: psycopg assumes an ISO DateStyle and a UTC session.
        assert_matches_original(self, None)

    def test_installed_django_matches_the_reviewed_fingerprint(self):
        # Fails when a Django 5.2.x release changes BaseTzLoader. apply() then
        # skips the patch, and this module needs a fresh review.
        self.assertEqual(
            db_psycopg.source_fingerprint(psycopg_any.BaseTzLoader),
            db_psycopg.EXPECTED_BASE_TZ_LOADER_SHA256,
        )

    def test_skips_and_warns_when_base_tz_loader_source_changed(self):
        class ChangedBaseTzLoader(PyTimestamptzLoader):
            timezone = None

            def load(self, data):  # pragma: no cover - never called
                return super().load(data)

        original_load = ChangedBaseTzLoader.load
        with mock.patch.object(psycopg_any, "BaseTzLoader", ChangedBaseTzLoader):
            with self.assertLogs("visiban.db_psycopg", level="WARNING") as logs:
                self.assertFalse(db_psycopg.apply())
        self.assertIn("source fingerprint", logs.output[0])
        self.assertIs(ChangedBaseTzLoader.load, original_load)
        self.assertNotIn("__init__", ChangedBaseTzLoader.__dict__)
        self.assertFalse(hasattr(ChangedBaseTzLoader, db_psycopg._APPLIED_ATTR))

    def test_skips_and_warns_when_source_is_unavailable(self):
        # e.g. an install that ships only .pyc files: no source, no patch.
        class UnreadableBaseTzLoader(PyTimestamptzLoader):
            timezone = None

        with (
            mock.patch.object(psycopg_any, "BaseTzLoader", UnreadableBaseTzLoader),
            mock.patch.object(db_psycopg.inspect, "getsource", side_effect=OSError),
            self.assertLogs("visiban.db_psycopg", level="WARNING") as logs,
        ):
            self.assertIsNone(db_psycopg.source_fingerprint(UnreadableBaseTzLoader))
            self.assertFalse(db_psycopg.apply())
        self.assertIn("fingerprint is None", logs.output[0])
        self.assertNotIn("load", UnreadableBaseTzLoader.__dict__)

    def test_noop_when_django_already_has_the_upstream_fix(self):
        # Django >= 6.1 (commit 994fa77) makes BaseTzLoader an adapt.Loader that
        # wraps the default loader itself. apply() must leave such a class alone.
        from psycopg import adapt

        class UpstreamBaseTzLoader(adapt.Loader):
            timezone = None

            def load(self, data):  # pragma: no cover - never called
                raise AssertionError

        # No warning: this is the expected, quiet exit (the issubclass check),
        # not the fingerprint-mismatch path, which would also return False.
        with (
            mock.patch.object(psycopg_any, "BaseTzLoader", UpstreamBaseTzLoader),
            self.assertNoLogs("visiban.db_psycopg", level="WARNING"),
        ):
            self.assertFalse(db_psycopg.apply())
        self.assertFalse(hasattr(UpstreamBaseTzLoader, db_psycopg._APPLIED_ATTR))
        self.assertNotIn("_visiban_default_loader", UpstreamBaseTzLoader.__dict__)


@skipUnless(
    connection.vendor == "postgresql" and psycopg_any.is_psycopg3,
    "the timestamptz loader only runs on PostgreSQL with psycopg 3",
)
class TimestamptzLoaderOnRealQueriesTests(TestCase):
    def test_values_and_errors_match_original_for_each_session_timezone(self):
        # A real psycopg connection, configured by Django, whose session TimeZone
        # the loaders read: UTC (USE_TZ=True), and the non-UTC zones a
        # USE_TZ=False install or a per-DATABASES TIME_ZONE would set.
        raw_conn = connection.get_new_connection(connection.get_connection_params())
        try:
            for session_tz in ("UTC", "America/New_York", "Asia/Kolkata"):
                raw_conn.execute("SELECT set_config('TimeZone', %s, false)", [session_tz])
                with self.subTest(session_tz=session_tz):
                    assert_matches_original(self, raw_conn)
        finally:
            raw_conn.close()

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
