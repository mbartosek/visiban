"""Test helpers for suites that change the account email as a side concern.

An email change on an account with a usable password needs the current
password in the same request (``accounts.credentials.
email_change_requires_current_password``), and allauth's manage-email page
needs a recent reauthentication for "Add" and "Make Primary". Suites whose
subject is something else (collision checks, pending changes, throttling) use
these helpers so each request does not have to repeat the password. The rule
itself is tested in ``test_email_change_reauth.py``.
"""

from rest_framework.test import APIClient

PROFILE_PATHS = ("/api/v1/auth/me/", "/api/v1/auth/user/")


class PasswordConfirmingAPIClient(APIClient):
    """``APIClient`` that adds ``current_password`` to profile writes carrying ``email``.

    Only for ``PATCH``/``PUT`` to the profile endpoints, only when the body is a
    dict with an ``email`` key, and never over a ``current_password`` the test
    set itself.
    """

    def __init__(self, *args, password=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_password = password

    def _with_password(self, path, data):
        if (
            self.current_password
            and isinstance(data, dict)
            and "email" in data
            and "current_password" not in data
            and path.split("?", 1)[0] in PROFILE_PATHS
        ):
            return {**data, "current_password": self.current_password}
        return data

    def patch(self, path, data=None, *args, **kwargs):
        return super().patch(path, self._with_password(path, data), *args, **kwargs)

    def put(self, path, data=None, *args, **kwargs):
        return super().put(path, self._with_password(path, data), *args, **kwargs)


def reauthenticate(client, password):
    """Confirm the password on allauth's reauthenticate page for *client*'s session."""
    response = client.post("/accounts/reauthenticate/", {"password": password})
    assert response.status_code == 302, response.status_code
    return response
