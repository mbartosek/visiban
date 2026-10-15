"""Origin validation for browser WebSocket handshakes.

Channels' stock ``OriginValidator`` refuses a handshake that carries no
``Origin`` header. Non-browser clients (ticket / token authenticated CLI,
native and agent clients, see #1109) never send one, so they must keep
working. This validator therefore only checks an Origin that is *present*:
it has to match the origins the deployment already configures. An absent
Origin falls straight through to the existing authentication middleware.

The allowed set is derived from settings that existing installs already
have, so no new configuration is required:

* ``CORS_ALLOWED_ORIGINS`` and ``CSRF_TRUSTED_ORIGINS`` (exact origins, the
  SPA's real origin in dev and prod), and
* any origin whose host matches ``ALLOWED_HOSTS`` (the same rule Django and
  Channels' ``AllowedHostsOriginValidator`` use). When ``ALLOWED_HOSTS``
  contains ``*`` every host matches, so the check is effectively open.
"""
from urllib.parse import urlsplit

from django.conf import settings
from django.http.request import validate_host


_DUPLICATE = object()


def _header(scope, name):
    """Return the header value, None if absent, or _DUPLICATE if repeated."""
    values = [v for k, v in scope.get("headers", []) if k == name]
    if not values:
        return None
    if len(values) > 1:
        return _DUPLICATE
    return values[0].decode("latin1")


def _normalize(origin):
    parts = urlsplit(origin)
    if not parts.scheme or not parts.hostname:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def origin_allowed(origin):
    """Return True when *origin* is acceptable for a WebSocket handshake."""
    parts = urlsplit(origin)
    # A valid Origin is scheme://host[:port] only: no userinfo, path, query
    # or fragment.
    if parts.username is not None or parts.password is not None:
        return False
    if parts.path or parts.query or parts.fragment:
        return False
    normalized = _normalize(origin)
    if normalized is None:
        return False
    configured = {
        _normalize(o)
        for o in [
            *getattr(settings, "CORS_ALLOWED_ORIGINS", []),
            *getattr(settings, "CSRF_TRUSTED_ORIGINS", []),
        ]
    }
    if normalized in configured:
        return True
    return validate_host(parts.hostname, settings.ALLOWED_HOSTS)


class OptionalOriginValidator:
    """ASGI middleware: refuse a present-but-unlisted Origin; allow no Origin."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return await self.inner(scope, receive, send)
        origin = _header(scope, b"origin")
        if origin is None or (origin is not _DUPLICATE and origin_allowed(origin)):
            return await self.inner(scope, receive, send)
        # Refuse the handshake before any application code runs.
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 403})
