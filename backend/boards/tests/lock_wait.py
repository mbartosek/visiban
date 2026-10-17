"""Assert that a test thread is blocked on a PostgreSQL lock (#1700).

The lock-race tests pause one request inside its transaction, start a second
one, and must prove the second *waits on the first's row lock* before they
release the first. They used to prove it with ``thread.join(0.5)`` followed by
``thread.is_alive()``, which only proves the thread was still running: on a
slow runner it can still be in client setup, admin rendering or form
validation when the holder is released, so the holder commits first and the
second request takes a code path the test never meant to exercise (the admin
form's ``ModelChoiceField`` rejecting a vanished swimlane before the
lock-guarded path runs, for one).

This module asks PostgreSQL instead. Every thread's backend pid is recorded
when Django opens its connection (``connection_created`` fires in the thread
that opens it), and ``assert_blocked_on_lock`` polls ``pg_stat_activity``
until *that* backend reports ``wait_event_type = 'Lock'``. The query is
scoped by pid, so another test database on the same server, or another
backend in this one, cannot satisfy it.

The probed thread must still call ``connections.close_all()`` before it exits
(backend CLAUDE.md, threaded tests); this module opens no connection of its
own and polls on the calling (main) thread's connection.

Not collected by pytest (no ``test_`` prefix); PostgreSQL only, like every
test that uses it.
"""

import threading
import time

from django.db import connection
from django.db.backends.signals import connection_created

DEFAULT_TIMEOUT = 10.0
POLL_INTERVAL = 0.02

# thread ident -> backend pid of the most recent connection that thread opened.
_backend_pids = {}
_lock = threading.Lock()


def _record_backend_pid(sender, connection, **kwargs):
    if connection.vendor != "postgresql":
        return
    pid = connection.connection.info.backend_pid
    with _lock:
        _backend_pids[threading.get_ident()] = pid


connection_created.connect(
    _record_backend_pid, dispatch_uid="boards.tests.lock_wait._record_backend_pid"
)


def _waiting_on_lock(pid):
    with connection.cursor() as cursor:
        # pg_stat_activity is snapshotted per transaction; drop the snapshot so
        # a caller inside an atomic block still sees fresh state on each poll.
        cursor.execute("SELECT pg_stat_clear_snapshot()")
        cursor.execute(
            "SELECT 1 FROM pg_stat_activity"
            " WHERE pid = %s AND datname = current_database()"
            " AND wait_event_type = 'Lock'",
            [pid],
        )
        return cursor.fetchone() is not None


def wait_until_blocked_on_lock(thread, *, timeout=DEFAULT_TIMEOUT):
    """Poll until ``thread``'s database backend waits on a lock.

    Returns True once PostgreSQL reports it waiting, False if the thread
    finishes first (it never blocked) or ``timeout`` elapses.
    """
    deadline = time.monotonic() + timeout
    while True:
        # Thread idents can be reused once a thread exits, but a reused ident
        # only maps to a stale pid until the new thread connects, and a closed
        # backend is not in pg_stat_activity: polling just continues.
        with _lock:
            pid = _backend_pids.get(thread.ident)
        if pid is not None and _waiting_on_lock(pid):
            return True
        if not thread.is_alive() or time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def assert_blocked_on_lock(testcase, thread, msg, *, timeout=DEFAULT_TIMEOUT):
    """Fail ``testcase`` unless ``thread`` is waiting on a PostgreSQL lock."""
    if not wait_until_blocked_on_lock(thread, timeout=timeout):
        state = "finished without waiting" if not thread.is_alive() else (
            f"was not waiting on a lock after {timeout}s"
        )
        testcase.fail(f"{msg} (thread {thread.name!r} {state})")
