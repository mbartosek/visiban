"""Env var rename helpers shared by settings.py and code that reads env outside it (#1873).

Env var names are a public contract (CLAUDE.md, "Settings and env vars"): a
name that has shipped is never renamed away. A rename adds the new name and
keeps the old one as an alias. This module is pure stdlib (no Django import),
so settings.py can import it while it is itself being imported, and so can a
management command, without an import cycle.

Call sites pass both names as string LITERALS. scripts/check-api-compat.py
(settings.py only) and scripts/helm-structure-check.sh section 3 (all of
backend/) find env reads by matching literals at the call site, so a name
passed through a variable is invisible to both gates.
"""

import logging
import os
import warnings

_logger = logging.getLogger("visiban.env_aliases")


def warn_deprecated_env_alias(old_name: str, new_name: str, *, removed: bool = False) -> None:
    """Warn at startup when an operator still sets a legacy env var name.

    Only fires when the old name is set to a non-empty value. Checking with
    defaults would warn on every worker start whether or not the alias is in
    use (#819).

    ``removed=False`` (the default) is for a working alias that is still read
    (see ``env_alias_name``). ``removed=True`` is for an alias that is no
    longer read at all, such as OIDC_SECRET since 1.1. Its message says the
    value has no effect, because "deprecated" would tell the operator that
    their setting still works when it silently does nothing.

    Both a DeprecationWarning and a log record are emitted. Python ignores a
    DeprecationWarning raised outside ``__main__`` by default, so the warning
    alone never reached a container log (#1873). While settings import,
    logging is not configured yet, so the record lands on stderr via logging's
    last-resort handler, the same route the dev CORS notice in settings uses.
    """
    if not os.environ.get(old_name):
        return
    if removed:
        message = f"{old_name} is no longer read and has no effect; replace it with {new_name}."
    else:
        message = (
            f"{old_name} is deprecated; rename it to {new_name}. "
            f"The {old_name} alias will be removed in a future release."
        )
    warnings.warn(message, DeprecationWarning, stacklevel=2)
    _logger.warning(message)


def env_alias_name(new_name: str, old_name: str) -> str:
    """Return which of a canonical env var and its working alias to read (#1873).

    For a rename whose old name must KEEP WORKING. The old names shipped in
    1.2 pre-releases, so an install that sets only the old name must behave
    exactly as before:

    - canonical name set to a non-empty value: it wins, even if the alias is
      also set (the operator has already migrated);
    - otherwise the alias, whenever it is present at all (an empty alias
      reads as empty, as it did before the rename);
    - otherwise the canonical name, so the caller's default applies.

    An empty canonical value is ignored rather than winning, so a compose-style
    ``${NEW:-}`` passthrough can never shadow an operator's legacy value.

    Pair every call with ``warn_deprecated_env_alias(old, new)`` for the
    startup warning.
    """
    if os.environ.get(new_name):
        return new_name
    if old_name in os.environ:
        return old_name
    return new_name
