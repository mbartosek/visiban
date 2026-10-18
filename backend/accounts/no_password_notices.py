"""Account notices for accounts without a usable password.

An account that signs in only through an identity provider has no password to
confirm, so the current-password and reauthentication checks on the account
email (``accounts.credentials.email_change_requires_current_password``,
``accounts.views.VisibanEmailView.post``) do not apply to it. These notices
keep the owner's existing mailboxes informed of changes to the account's
addresses and credentials:

- An address added to or confirmed on the account: a notice to the account's
  current email (``User.email``, the primary address), never to the address
  that was added.
- A first password set, from a reset link or directly by the signed-in user: a notice to the account email and
  every verified address on the account, one message per address.

No extra proof is asked for: an account owner who has lost the primary
mailbox must still be able to add an address and recover the account. The
notices are the control. Accounts with a usable password are unaffected; they
are already asked for the password.

Every notice is sent with ``on_commit``, so a change that rolls back mails
nobody, and is fixed text (``accounts.email_change.send_fixed_text_notice``).
Email addresses are personal data: nothing here logs one.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

logger = logging.getLogger(__name__)

ADDRESS_ADDED_TEMPLATE = "accounts/email/email_address_added_notice"
FIRST_PASSWORD_TEMPLATE = "accounts/email/first_password_set_notice"
ADDRESS_NOTICE_CACHE_PREFIX = "accounts:address_notice:"
ADDRESS_NOTICE_WINDOW = 60 * 60

# Set on the ``EmailAddress`` instance once an add notice is scheduled, so that
# allauth's ``verify_email``, which sends ``email_added`` and then
# ``email_confirmed`` for the same instance when it creates and verifies a row
# in one step, results in one notice rather than two.
_NOTICE_SCHEDULED_ATTR = "_visiban_address_notice_scheduled"
_FIRST_PASSWORD_ATTR = "_visiban_first_password_notice_scheduled"


def _normalized(email) -> str:
    return (email or "").strip().lower()


def notify_primary_of_address_change(user, address_email) -> None:
    """Schedule the address notice to *user*'s current email, if one applies.

    Nothing is sent when the account has a usable password, has no email, or
    when *address_email* is the account email itself (a signup confirmation,
    or a row allauth has just made primary).

    The recipient is read now and re-checked when the transaction commits: if
    ``User.email`` has moved off it by then (a confirmed email change), the
    previous-address notice (``accounts.email_change.
    send_previous_address_notice``) already reaches that mailbox, so this one
    is skipped rather than sending two mails about the same step.

    At most one such notice is sent per account per hour (the text is generic,
    so more adds nothing). An account with an empty ``User.email`` has no
    primary to notify (known limit).
    """
    if user is None or user.has_usable_password():
        return
    primary = user.email or ""
    if not _normalized(primary) or _normalized(primary) == _normalized(address_email):
        return
    user_id = user.pk

    def _send():
        from django.contrib.auth import get_user_model

        from .email_change import send_fixed_text_notice

        current = (
            get_user_model().objects.filter(pk=user_id).values_list("email", flat=True).first()
        )
        if current is None or _normalized(current) != _normalized(primary):
            return
        # Claimed at send time, after the commit, so a rolled-back change does
        # not use up the hour. If the cache is unavailable the notice is sent.
        try:
            if not cache.add(f"{ADDRESS_NOTICE_CACHE_PREFIX}{user_id}", 1, ADDRESS_NOTICE_WINDOW):
                return
        except Exception as exc:  # noqa: BLE001 - fail open: a notice beats silence
            logger.warning(
                "no_password_notice.throttle_unavailable user_id=%s exc=%s",
                user_id,
                type(exc).__name__,
            )
        send_fixed_text_notice(user_id, primary, ADDRESS_ADDED_TEMPLATE)

    transaction.on_commit(_send)


def _account_for(email_address, request):
    """The account *email_address* belongs to: at most one pk lookup, only when the user is not already loaded.

    The signals run on every add and confirm, for every account, and most
    accounts have a usable password and get no notice; so the user already
    attached to the row, or the signed-in ``request.user`` when it is the same
    account, is used first, and the row is read only when neither is at hand
    (a confirmation link followed while signed out). The on-commit callback in
    ``notify_primary_of_address_change`` re-reads the email before sending.
    """
    from allauth.account.models import EmailAddress
    from django.contrib.auth import get_user_model

    if EmailAddress.user.is_cached(email_address):
        return email_address.user
    request_user = getattr(request, "user", None)
    if (
        request_user is not None
        and getattr(request_user, "is_authenticated", False)
        and request_user.pk == email_address.user_id
    ):
        return request_user
    return get_user_model().objects.filter(pk=email_address.user_id).first()


def _log_failure(email_address, exc) -> None:
    logger.warning(
        "no_password_notice.schedule_failed user_id=%s exc=%s",
        getattr(email_address, "user_id", None),
        type(exc).__name__,
    )


def notify_on_email_added(sender, request, user, email_address, **kwargs):
    """allauth ``email_added`` receiver (the ``/accounts/email/`` "Add" action).

    Both an added and a confirmed address get a notice. Adding alone does not
    make an address usable for a reset link on these accounts (that needs a
    verified address, ``accounts.forms.password_reset_still_allowed``), but it
    is the earliest point the owner can be told; confirming is the step that
    makes it usable, and it can happen without an add notice (a pending change
    under ``EMAIL_VERIFICATION=mandatory``, or an address that was already on
    the account). The two are separate requests, at most minutes apart, and
    both are bounded by allauth's ``manage_email`` and ``confirm_email`` rate
    limits. Never raises: allauth sends its signals with a plain ``send()``.
    """
    try:
        notify_primary_of_address_change(_account_for(email_address, request), email_address.email)
        setattr(email_address, _NOTICE_SCHEDULED_ATTR, True)
    except Exception as exc:  # noqa: BLE001 - a notice must never fail the add
        _log_failure(email_address, exc)


def notify_on_email_confirmed(sender, request, email_address, **kwargs):
    """allauth ``email_confirmed`` receiver: every route that verifies an address.

    Covers the REST verify-email endpoint, allauth's own confirmation page and
    the ``EmailAddress`` admin's verify action. Connected ahead of
    ``accounts.email_change.apply_confirmed_email_change`` so the recipient is
    read before a confirmed email change moves ``User.email``. Never raises.
    """
    if getattr(email_address, _NOTICE_SCHEDULED_ATTR, False):
        return
    try:
        notify_primary_of_address_change(_account_for(email_address, request), email_address.email)
    except Exception as exc:  # noqa: BLE001 - a notice must never fail the confirmation
        _log_failure(email_address, exc)


# At most this many first-password notices per password write. Each send runs in turn
# after commit and can wait up to NOTIFICATION_EMAIL_TIMEOUT, so the list is
# bounded; the account email and the longest-held addresses are kept, since
# they are the likeliest to be the owner's.
FIRST_PASSWORD_NOTICE_LIMIT = 10


def notify_verified_addresses_of_first_password(user) -> None:
    """Schedule a notice to the account's addresses once a first password is set (reset link or signed-in user).

    Called by ``accounts.credentials.finalize_password_change`` (and so by
    ``finalize_password_reset``) when the account had no usable password before
    the write, whether set from a reset link or by the signed-in user; the
    caller reads that from the row it locked before the save. Recipients are the account email (``User.email``, first,
    whether or not it has a verified row) and every verified address, oldest
    first, without duplicates and capped at ``FIRST_PASSWORD_NOTICE_LIMIT``.
    Every one is mailed, not only the address a reset link went to, because
    any of them may be the one the owner still reads. The list is read in the
    caller's transaction and sent on commit, one message per address.
    """
    from allauth.account.models import EmailAddress

    # One notice per user object: a route may finalize more than once (the
    # allauth adapter hook and the form around it), and a password write that
    # reaches this through two of them must not mail twice.
    if getattr(user, _FIRST_PASSWORD_ATTR, False):
        return
    setattr(user, _FIRST_PASSWORD_ATTR, True)
    seen = set()
    recipients = []
    candidates = [user.email or ""]
    candidates += list(
        EmailAddress.objects.filter(user_id=user.pk, verified=True)
        .order_by("pk")
        .values_list("email", flat=True)
    )
    for email in candidates:
        key = _normalized(email)
        if key and key not in seen:
            seen.add(key)
            recipients.append(email.strip())
    recipients = recipients[:FIRST_PASSWORD_NOTICE_LIMIT]
    if not recipients:
        return
    user_id = user.pk

    def _send():
        from django.core.mail import get_connection

        from .email_change import send_fixed_text_notice

        # One connection for the whole loop rather than one per recipient.
        # Never raises: a failure here is logged by type only.
        connection = None
        try:
            connection = get_connection(
                fail_silently=False,
                timeout=getattr(settings, "NOTIFICATION_EMAIL_TIMEOUT", 10),
            )
            for email in recipients:
                send_fixed_text_notice(user_id, email, FIRST_PASSWORD_TEMPLATE, connection)
        except Exception as exc:  # noqa: BLE001 - a notice must never fail the password write
            logger.warning(
                "no_password_notice.send_failed user_id=%s exc=%s", user_id, type(exc).__name__
            )
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - closing is best-effort
                    pass

    transaction.on_commit(_send)
