"""Operator password reset that also revokes credentials issued under the old password.

Replaces Django's ``changepassword`` (the documented operator reset path) with
the same arguments, prompts and validation. The one difference is the final
step: the password save is followed, in the same transaction, by
``revoke_credentials_after_operator_reset``, so Personal Access Tokens and the
DRF ``Token`` do not outlive the password they were issued under. Registry
Rule 3 in ``docs/development/security-invariants.md`` lists this caller.
"""

import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.management.commands import (
    changepassword as django_changepassword,
)
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import transaction

from accounts.credentials import lock_user_row, revoke_credentials_after_operator_reset


class Command(django_changepassword.Command):
    help = (
        "Change a user's password and revoke the credentials issued under the old one."
    )

    def handle(self, *args, **options):
        user_model = get_user_model()
        username = options["username"] or getpass.getuser()

        try:
            u = user_model._default_manager.using(options["database"]).get(
                **{user_model.USERNAME_FIELD: username}
            )
        except user_model.DoesNotExist:
            raise CommandError("user '%s' does not exist" % username)

        self.stdout.write("Changing password for user '%s'" % u)

        max_tries = 3
        count = 0
        p1, p2 = 1, 2  # force an initial mismatch
        password_validated = False
        while (p1 != p2 or not password_validated) and count < max_tries:
            p1 = self._get_pass()
            p2 = self._get_pass("Password (again): ")
            if p1 != p2:
                self.stdout.write("Passwords do not match. Please try again.")
                count += 1
                continue
            try:
                validate_password(p2, u)
            except ValidationError as err:
                self.stderr.write("\n".join(err.messages))
                count += 1
            else:
                password_validated = True

        if count == max_tries:
            raise CommandError(
                "Aborting password change for user '%s' after %s attempts" % (u, count)
            )

        # The prompts above run outside any transaction; only the save and the
        # revocation share one, and the row is re-read under its lock so the
        # write does not overwrite columns changed while the operator typed.
        with transaction.atomic():
            u = lock_user_row(u)
            u.set_password(p1)
            u.save(update_fields=["password"])
            revoke_credentials_after_operator_reset(u)

        return "Password changed successfully for user '%s'" % u
