"""Give a test session the provider sign-in record a first password needs (#1260).

Setting a first password on an account with no usable password needs a recent
provider sign-in to that account, recorded in the session
(``accounts.credentials.session_recently_signed_in_by_provider``). A test that
signs in with ``force_login`` has no such record, so it calls
``sign_in_through_provider`` after ``force_login`` to write the record allauth's
social-login flow would have written.
"""

import time

from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.socialaccount.models import SocialAccount


def link_identity(user, provider="google", uid=None):
    """Link a provider identity to *user* and return its (provider, uid)."""
    uid = uid or f"{provider}-{user.pk}"
    SocialAccount.objects.get_or_create(
        user=user, provider=provider, uid=uid, defaults={"extra_data": {}}
    )
    return provider, uid


def record_provider_sign_in(client, provider, uid, *, seconds_ago=0):
    """Append a ``socialaccount`` authentication record to *client*'s session."""
    session = client.session
    records = list(session.get(AUTHENTICATION_METHODS_SESSION_KEY, []))
    records.append(
        {"method": "socialaccount", "at": time.time() - seconds_ago, "provider": provider, "uid": uid}
    )
    session[AUTHENTICATION_METHODS_SESSION_KEY] = records
    session.save()


def sign_in_through_provider(client, user, provider="google", *, seconds_ago=0):
    """Link an identity to *user* and record a sign-in with it in *client*'s session.

    Call after ``client.force_login(user)``.
    """
    provider, uid = link_identity(user, provider)
    record_provider_sign_in(client, provider, uid, seconds_ago=seconds_ago)
