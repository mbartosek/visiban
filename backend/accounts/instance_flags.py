"""Instance-wide flags served on two surfaces: ``/auth/user/`` and ``/auth/site-config/``.

``CurrentUserSerializer`` carries these five flags as frozen 1.x fields (#1664), and
``SiteConfigView`` carries the same five (#1872), because ``site-config`` is where
instance-level state belongs. Both surfaces read the values from here, so the two
cannot drift: one computation, two serializations.

WHY ALL FIVE ARE SAFE ON THE ANONYMOUS ``site-config`` ENDPOINT (#1872). Every value
below is a property of the instance, never of the caller. None reads ``request`` or
``request.user``, and none varies by role or permission. Each one is also meant for
every user of the instance, and was already visible to anonymous clients in practice:

* ``maintenance_mode`` / ``maintenance_message``: while maintenance is on, any
  anonymous non-exempt write (e.g. ``POST /api/v1/auth/registration/``) is answered
  503 with this exact notice as ``detail`` (``visiban.middleware.MaintenanceModeMiddleware``).
  That holds with ``DEMO_MODE`` on too: the maintenance check runs in ``__call__``,
  before ``DemoModeMiddleware.process_view`` can refuse the write. The admin docs say
  the notice is public, so operators do not write internal detail into it.
* ``demo_mode`` / ``demo_next_reset_at``: already on ``site-config`` since #1034 and #1179.
  The login page needs them before anyone signs in.
* ``git_lens_enabled``: the ``GIT_LENS_ENABLED`` setting, the same value for every
  user. The ``git-lens/`` routes are mounted only when it is on (``visiban/urls.py``),
  so an anonymous probe already gets a 401 or a 404 that reveals it. Per-board lens
  state (connections, provider, repository) stays behind board membership on the
  lens endpoints and is not exposed here.

A future flag that depends on the caller (role, membership, per-user preference) must
NOT be added here, because ``site-config`` serves this dict to anyone.
"""
from __future__ import annotations

from django.conf import settings

from visiban.demo import demo_next_reset_at_iso

from .models import get_maintenance_message, get_maintenance_state


def git_lens_enabled() -> bool:
    """Whether the Issue Board Lens is enabled for this install (``GIT_LENS_ENABLED``)."""
    return bool(getattr(settings, "GIT_LENS_ENABLED", False))


def demo_mode() -> bool:
    """Whether the instance runs as the public hosted demo (``DEMO_MODE``)."""
    return bool(getattr(settings, "DEMO_MODE", False))


def demo_next_reset_at() -> str | None:
    """ISO 8601 UTC of the next demo reset while ``DEMO_MODE`` is on, else None."""
    return demo_next_reset_at_iso()


def maintenance_notice() -> tuple[bool, str]:
    """``(active, message)`` exactly as clients see it.

    The message is always non-empty while maintenance is active, because the built-in
    default replaces a blank operator notice server-side. It is ``""`` otherwise, so a
    stale notice from an earlier window never reaches a client. One cached read
    (``get_maintenance_state``) answers both values.
    """
    active, message = get_maintenance_state()
    return active, (get_maintenance_message(message) if active else "")


def instance_flags() -> dict:
    """The five shared flags, keyed by their wire names on both endpoints."""
    active, message = maintenance_notice()
    return {
        "git_lens_enabled": git_lens_enabled(),
        "maintenance_mode": active,
        "maintenance_message": message,
        "demo_mode": demo_mode(),
        "demo_next_reset_at": demo_next_reset_at(),
    }
