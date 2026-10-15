"""Shared failed-attempt budget for endpoints that verify the current password.

Failed ``current_password`` checks draw on the same per-account allauth
``login_failed`` bucket as failed logins, so the two share one budget and an
account locked out at login is refused here too. Mirrors allauth's own
``authenticate()`` sequence: consume one unit up front, roll it back when the
password is correct, keep it otherwise.
"""
from allauth.account.app_settings import RATE_LIMITS
from allauth.core.internal import ratelimit
from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site
from rest_framework.exceptions import Throttled

from .adapter import account_lockout_cache_seed


def begin_current_password_check(request, user):
    """Consume one unit of the account's failed-attempt budget.

    Returns a usage handle to pass to ``current_password_accepted`` when the
    password turns out to be correct, or ``None`` when no budget applies (the
    shared demo account, which login also exempts so visitors cannot lock it).
    Raises ``Throttled`` (429) when the account is currently locked out.
    """
    if settings.DEMO_MODE:
        demo_username = settings.DEMO_LOGIN_USERNAME.strip().lower()
        if demo_username and user.get_username().strip().lower() == demo_username:
            return None
    seed = account_lockout_cache_seed(get_current_site(request).domain, user)
    usage = ratelimit.consume(request, config=RATE_LIMITS, action="login_failed", key=seed)
    if not usage:
        raise Throttled(detail="Too many failed attempts. Try again later.")
    return usage


def current_password_accepted(usage):
    """Give the unit back once the current password was verified."""
    if usage:
        usage.rollback()
