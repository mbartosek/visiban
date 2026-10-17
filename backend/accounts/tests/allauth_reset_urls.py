"""Test URLconf: allauth's own password-reset views mounted ahead of the project's redirects.

The project redirects allauth's reset pages to the SPA (#1554), but the forms
in ``ACCOUNT_FORMS`` (``VisibanResetPasswordForm`` and
``VisibanResetPasswordKeyForm``), the token generator and the ``password_reset``
signal receiver are kept as defense in depth for a URLconf that mounts allauth's
views (an extension package, say). Tests of that layer use this URLconf via
``override_settings(ROOT_URLCONF="accounts.tests.allauth_reset_urls")``.
"""

from django.urls import path, re_path

from allauth.account import views as allauth_views

from visiban import urls as project_urls

urlpatterns = [
    path("accounts/password/reset/", allauth_views.password_reset, name="account_reset_password"),
    path("accounts/password/reset/done/", allauth_views.password_reset_done, name="account_reset_password_done"),
    re_path(
        r"^accounts/password/reset/key/(?P<uidb36>[0-9A-Za-z]+)-(?P<key>.+)/$",
        allauth_views.password_reset_from_key,
        name="account_reset_password_from_key",
    ),
    path(
        "accounts/password/reset/key/done/",
        allauth_views.password_reset_from_key_done,
        name="account_reset_password_from_key_done",
    ),
    *project_urls.urlpatterns,
]
