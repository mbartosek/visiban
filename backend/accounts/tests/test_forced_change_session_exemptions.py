"""Session surfaces: which views a pending account may still reach (#1561).

Registry Rule 1 (``docs/development/security-invariants.md``).
``visiban.middleware.PendingAccountActionMiddleware`` gates ``/accounts/`` and
``/admin/`` for an account with a forced password or username change pending,
and lets through only the views in ``pending_action_exempt_views()``, decided
on the resolved view. This module covers:

- the middleware's decision function and its predicate (the shared gates in
  ``visiban.authorization``);
- the SSO round trip: sign-in completes and lands on the SPA, while a connect
  attempt is refused by ``SocialRegistrationAdapter.pre_social_login``;
- the extension setting ``PENDING_ACTION_EXTRA_EXEMPT_VIEWS``;
- the ``accounts.E005``, ``accounts.E006`` and ``accounts.E007`` system checks;
- ordering with the maintenance and demo middleware.

The per-route decision table for every ``/accounts/`` and ``/admin/`` route is
in ``test_auth_entry_points.py``.
"""

import functools
import sys
import types
from unittest import mock
from urllib.parse import urlsplit

from allauth.account.models import EmailAddress
from allauth.core import context as allauth_context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.internal.flows.login import complete_login, pre_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import include
from django.urls import path as url_path
from django.urls import re_path

from accounts.checks import (
    check_pending_action_extra_exempt_views,
    check_pending_action_extra_exempt_views_not_oss_gated,
    check_pending_action_middleware_installed,
)
from accounts.models import SiteSetting, User
from visiban import authorization
from visiban.demo import DEMO_READ_ONLY_CODE
from visiban.middleware import (
    PENDING_ACTION_REFUSAL,
    PendingAccountActionMiddleware,
    _regex_literal_prefix,
    _valid_extra_gated_prefixes,
    _view_name_of,
    is_pending_action_exempt_view,
    is_well_formed_exempt_view,
    oss_gated_view_names,
    pending_action_exempt_views,
    resolved_view_name,
)


def _saml_acs(request):
    return HttpResponse("ok")


# An extension-style URLconf: its views live outside the OSS gated prefixes.
urlpatterns = [
    url_path("sso/acs/", _saml_acs, name="saml_acs"),
    url_path("sso/other/", _saml_acs, name="saml_other"),
]

# A URLconf with an OSS-prefixed namespaced include and an unnamed route.
_gated = types.ModuleType(f"{__name__}_gated_urls")
_inner = [url_path("inner/", _saml_acs, name="inner")]
_gated.urlpatterns = [
    url_path("admin/", include((_inner, "gated"), namespace="gated")),
    url_path("accounts/unnamed/", _saml_acs),
    url_path("elsewhere/", _saml_acs, name="elsewhere"),
]
sys.modules[_gated.__name__] = _gated

FRONTEND = "https://app.example.test"
PASSWORD = "Sess-Gate-Passw0rd!"  # gitleaks:allow -- test-only fixture password, not a credential
PROVIDERS = {
    "google": {"APP": {"client_id": "g-id", "secret": "g-secret"}},
    "github": {"APP": {"client_id": "gh-id", "secret": "gh-secret"}},
}
PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
GATE = "visiban.middleware.PendingAccountActionMiddleware"
AUTH = "django.contrib.auth.middleware.AuthenticationMiddleware"
FLAGS = ({"must_change_password": True}, {"must_change_username": True})


def _passthrough(request):
    return HttpResponse("view ran")


def _call_gate(path, user, method="GET"):
    request = RequestFactory().generic(method, path)
    request.user = user
    return PendingAccountActionMiddleware(_passthrough)(request)


# ---------------------------------------------------------------------------
# Decision function
# ---------------------------------------------------------------------------


@override_settings(FRONTEND_URL=FRONTEND)
class ExemptionDecisionTests(SimpleTestCase):
    def test_resolved_view_names(self):
        self.assertEqual(resolved_view_name("/accounts/logout/"), "account_logout")
        self.assertEqual(resolved_view_name("/admin/logout/"), "admin:logout")
        self.assertEqual(resolved_view_name("/accounts/google/login/callback/"), "google_callback")
        self.assertEqual(
            resolved_view_name("/accounts/confirm-email/abc:def/"),
            "accounts.views.EmailConfirmRedirectView",
        )
        self.assertIsNone(resolved_view_name("//admin/"))

    def test_slashless_path_is_judged_by_its_slashed_route(self):
        self.assertTrue(is_pending_action_exempt_view("/accounts/logout"))
        self.assertFalse(is_pending_action_exempt_view("/accounts/email"))

    def test_sso_round_trip_of_each_installed_provider_is_exempt(self):
        exempt = pending_action_exempt_views()
        for name in ("google_login", "google_callback", "github_login", "github_callback",
                     "gitlab_login", "gitlab_callback", "openid_connect_login", "openid_connect_callback"):
            with self.subTest(name=name):
                self.assertIn(name, exempt)
        # Other per-provider views are not part of the round trip.
        self.assertNotIn("google_login_by_token", exempt)
        self.assertNotIn("account_login", exempt)

    def test_capability_bearing_views_are_not_exempt(self):
        for path in (
            "/accounts/email/",
            "/accounts/3rdparty/",
            "/accounts/reauthenticate/",
            "/accounts/password/change/",
            "/accounts/password/set/",
            "/accounts/google/login/token/",
            "/admin/",
            "/admin/accounts/user/1/password/",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_pending_action_exempt_view(path))

    def test_each_flag_redirects_gated_get_and_refuses_other_methods(self):
        for flags in FLAGS:
            user = User(username="p", **flags)
            with self.subTest(flags=flags):
                r = _call_gate("/accounts/email/", user)
                self.assertEqual((r.status_code, r["Location"]), (302, FRONTEND))
                r = _call_gate("/accounts/email/", user, "POST")
                self.assertEqual(r.status_code, 403)
                self.assertEqual(r.content.decode(), PENDING_ACTION_REFUSAL)
                self.assertEqual(_call_gate("/accounts/logout/", user, "POST").content, b"view ran")
                self.assertEqual(
                    _call_gate("/accounts/google/login/callback/", user).content, b"view ran"
                )

    def test_clear_or_anonymous_users_are_not_gated(self):
        for user in (User(username="ok"), AnonymousUser()):
            with self.subTest(user=user):
                self.assertEqual(_call_gate("/admin/", user).content, b"view ran")

    def test_predicate_is_the_shared_forced_change_gates(self):
        """The middleware evaluates visiban.authorization's gates, not a copy."""
        user = User(username="p", must_change_password=True)
        with mock.patch.object(
            authorization, "_evaluate_account_gates", wraps=authorization._evaluate_account_gates
        ) as spy:
            _call_gate("/admin/", user)
        spy.assert_called_once()
        exempt = spy.call_args.args[1]
        self.assertEqual(frozenset(authorization.ACCOUNT_GATES) - exempt, authorization.FORCED_CHANGE_GATES)

    def test_redirect_target_ignores_request_data(self):
        user = User(username="p", must_change_password=True)
        request = RequestFactory().get(
            "/admin/?next=https://evil.example/", HTTP_HOST="evil.example", HTTP_REFERER="https://evil.example/"
        )
        request.user = user
        r = PendingAccountActionMiddleware(_passthrough)(request)
        self.assertEqual(r["Location"], FRONTEND)

    @override_settings(FORCE_SCRIPT_NAME="/visiban")
    def test_subpath_deployment_is_judged_on_path_info(self):
        user = User(username="p", must_change_password=True)
        request = RequestFactory().get("/admin/", SCRIPT_NAME="/visiban")
        request.user = user
        self.assertEqual(request.path_info, "/admin/")
        r = PendingAccountActionMiddleware(_passthrough)(request)
        self.assertEqual(r["Location"], FRONTEND)


# ---------------------------------------------------------------------------
# Extension setting
# ---------------------------------------------------------------------------


@override_settings(FRONTEND_URL=FRONTEND)
class ExtraExemptViewsTests(SimpleTestCase):
    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("saml_acs",))
    def test_an_extra_view_name_is_exempt(self):
        self.assertTrue(is_pending_action_exempt_view("/sso/acs/", urlconf=__name__))
        self.assertFalse(is_pending_action_exempt_view("/sso/other/", urlconf=__name__))
        self.assertEqual(check_pending_action_extra_exempt_views(None), [])
        self.assertEqual(check_pending_action_extra_exempt_views_not_oss_gated(None), [])

    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS="saml_acs")
    def test_a_single_string_is_one_name(self):
        self.assertTrue(is_pending_action_exempt_view("/sso/acs/", urlconf=__name__))
        self.assertFalse(is_pending_action_exempt_view("/admin/"))

    def test_oss_gated_view_names_cover_args_and_namespaces(self):
        names = oss_gated_view_names()
        for name in ("account_email", "admin:index", "admin:accounts_user_change", "account_logout"):
            with self.subTest(name=name):
                self.assertIn(name, names)
        self.assertNotIn("saml_acs", names)

    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("account_email", "admin:index", "admin:accounts_user_change"))
    def test_oss_gated_names_are_rejected(self):
        errors = check_pending_action_extra_exempt_views_not_oss_gated(None)
        self.assertEqual([e.id for e in errors], ["accounts.E007"] * 3)

    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("account_logout", "saml_acs"))
    def test_repeating_an_already_exempt_oss_view_is_allowed(self):
        self.assertEqual(check_pending_action_extra_exempt_views_not_oss_gated(None), [])

    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("account_email", "admin:index", "saml_acs"))
    def test_gated_name_is_ignored_at_runtime_regardless_of_the_check(self):
        # The middleware never runs system checks, so this is the enforcement
        # that holds under gunicorn/ASGI or --skip-checks.
        self.assertFalse(is_pending_action_exempt_view("/accounts/email/"))
        self.assertFalse(is_pending_action_exempt_view("/admin/"))
        self.assertTrue(is_pending_action_exempt_view("/sso/acs/", urlconf=__name__))

    def test_view_name_of_handles_unnamed_and_callable_instances(self):
        from django.urls import URLPattern, path

        class Callable:
            def __call__(self, request):
                return HttpResponse("ok")

        unnamed = path("x/", _saml_acs)
        self.assertEqual(_view_name_of(unnamed), f"{__name__}._saml_acs")
        inst = URLPattern(unnamed.pattern, Callable(), name=None)
        self.assertEqual(_view_name_of(inst), f"{__name__}.{Callable.__qualname__}")
        partial = path("y/", functools.partial(_saml_acs))
        self.assertEqual(_view_name_of(partial), f"{__name__}._saml_acs")

    def test_walk_names_nested_namespaced_includes_and_unnamed_routes(self):
        names = oss_gated_view_names(f"{__name__}_gated_urls")
        self.assertEqual(names, {"gated:inner", f"{__name__}._saml_acs"})

    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("*", "no_such_view"))
    def test_malformed_and_unknown_names_are_not_e007(self):
        self.assertEqual(check_pending_action_extra_exempt_views_not_oss_gated(None), [])

    @override_settings(
        PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("*", "admin:*", "/accounts/email/", "account email", "", None)
    )
    def test_malformed_entries_are_ignored_and_reported(self):
        self.assertFalse(is_pending_action_exempt_view("/accounts/email/"))
        self.assertFalse(is_pending_action_exempt_view("/admin/"))
        errors = check_pending_action_extra_exempt_views(None)
        self.assertEqual([e.id for e in errors], ["accounts.E006"] * 6)

    def test_well_formed_names(self):
        for name in ("saml_acs", "admin:logout", "accounts.views.SomeView", "x-y"):
            with self.subTest(name=name):
                self.assertTrue(is_well_formed_exempt_view(name))

    def test_default_is_empty_and_passes(self):
        self.assertEqual(tuple(settings.PENDING_ACTION_EXTRA_EXEMPT_VIEWS), ())
        self.assertEqual(check_pending_action_extra_exempt_views(None), [])


# ---------------------------------------------------------------------------
# The URL walk behind accounts.E007: non-literal mounts and extra prefixes (#1593)
# ---------------------------------------------------------------------------

# Mounts whose printed route does not start with a gated prefix but which can
# serve paths under one, next to mounts that provably cannot.
_mounts = types.ModuleType(f"{__name__}_mount_urls")
_mounts.urlpatterns = [
    # Regex alternation: serves /accounts/alt/ and /admin/alt/.
    re_path(r"^(?:accounts|admin)/", include([url_path("alt/", _saml_acs, name="regex_alternation")])),
    # Leading converter: <str:section> can be "accounts".
    url_path("<str:section>/", include([url_path("conv/", _saml_acs, name="converter_include")])),
    url_path("<slug:section>/leaf/", _saml_acs, name="converter_leaf"),
    # An int or uuid cannot be "accounts" or "admin".
    url_path("<int:pk>/num/", _saml_acs, name="int_leading"),
    url_path("<uuid:pk>/u/", _saml_acs, name="uuid_leading"),
    # A literal prefix that diverges from every gated prefix.
    re_path(r"^sso/(?P<x>\w+)/$", _saml_acs, name="regex_elsewhere"),
    re_path(r"^accountsx/$", _saml_acs, name="literal_lookalike"),
    # A regex whose reach cannot be bounded without matching past the gated
    # prefix: counted (fail safe), although this one cannot in fact serve it.
    re_path(r"^[b-z]{8}/q/$", _saml_acs, name="unbounded_regex"),
    # Unanchored: Django applies it with search(), so it can follow /accounts/.
    re_path(r"anywhere/", include([url_path("x/", _saml_acs, name="unanchored")])),
    # path converter: can span /accounts/.
    url_path("<path:rest>/p/", _saml_acs, name="path_converter"),
    # Slashless route: /admin is gated like /admin/.
    url_path("admin", _saml_acs, name="slashless_admin"),
    # Literal endpoint regexes with no end anchor: Django applies them with
    # search(), so ^acc serves /accounts/x and ^admi serves /admin/x.
    re_path(r"^acc", _saml_acs, name="open_literal_acc"),
    re_path(r"^admi", _saml_acs, name="open_literal_admi"),
    # Literals followed by a top-level alternation or a lookahead.
    re_path(r"^ac|zz/$", _saml_acs, name="literal_then_alternation"),
    re_path(r"^ad(?=min)(?:min)/x/$", _saml_acs, name="literal_then_lookahead"),
]
sys.modules[_mounts.__name__] = _mounts

# A root URLconf with an extension point, like visiban/urls.py.
_ext = types.ModuleType(f"{__name__}_ext_urls")
_ext.extension_urlpatterns = [url_path("sso/acs/", _saml_acs, name="ext_acs")]
_ext.urlpatterns = [
    url_path("sso/oss/", _saml_acs, name="oss_under_sso"),
    url_path("accounts/ext/", _saml_acs, name="oss_under_accounts"),
    *_ext.extension_urlpatterns,
    # Open-ended literal endpoint regex: serves /sso/anything (after ext_acs so
    # it does not shadow /sso/acs/).
    re_path(r"^ss", _saml_acs, name="oss_open_literal_ss"),
    url_path("accounts/mine/", _saml_acs, name="ext_under_accounts"),
]
_ext.extension_urlpatterns.append(_ext.urlpatterns[-1])
sys.modules[_ext.__name__] = _ext


class GatedViewWalkTests(SimpleTestCase):
    def setUp(self):
        oss_gated_view_names.cache_clear()
        self.addCleanup(oss_gated_view_names.cache_clear)

    def test_regex_and_converter_mounts_that_can_serve_a_gated_path_are_named(self):
        names = oss_gated_view_names(_mounts.__name__)
        for name in (
            "regex_alternation",
            "converter_include",
            "converter_leaf",
            "unanchored",
            "path_converter",
            "slashless_admin",
        ):
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_open_ended_literal_endpoint_regexes_are_named(self):
        names = oss_gated_view_names(_mounts.__name__)
        for name in (
            "open_literal_acc",
            "open_literal_admi",
            "literal_then_alternation",
            "literal_then_lookahead",
        ):
            with self.subTest(name=name):
                self.assertIn(name, names)

    def test_open_ended_literal_endpoint_regexes_really_serve_gated_paths(self):
        # Resolve order puts earlier mounts first, so use paths only these reach.
        self.assertEqual(resolved_view_name("/accounts/x", _mounts.__name__), "open_literal_acc")
        self.assertEqual(resolved_view_name("/admin/x", _mounts.__name__), "open_literal_admi")

    def test_regex_parse_failure_fails_safe(self):
        with mock.patch("re._parser.parse", side_effect=ValueError("boom")):
            self.assertEqual(_regex_literal_prefix(r"^accountsx/$"), ("", False, False))

    def test_regex_literal_prefix_reports_the_end_anchor(self):
        self.assertEqual(_regex_literal_prefix(r"^accounts/x/$"), ("accounts/x/", True, True))
        self.assertEqual(_regex_literal_prefix(r"^acc"), ("acc", True, False))
        self.assertEqual(_regex_literal_prefix(r"^sso/(?P<x>\w+)/$"), ("sso/", False, False))

    def test_mounts_that_cannot_serve_a_gated_path_are_not_named(self):
        names = oss_gated_view_names(_mounts.__name__)
        for name in ("int_leading", "uuid_leading", "regex_elsewhere", "literal_lookalike"):
            with self.subTest(name=name):
                self.assertNotIn(name, names)

    def test_a_regex_that_cannot_be_bounded_is_named_fail_safe(self):
        self.assertIn("unbounded_regex", oss_gated_view_names(_mounts.__name__))

    def test_the_named_mounts_really_resolve_under_a_gated_prefix(self):
        for path, name in (
            ("/accounts/alt/", "regex_alternation"),
            ("/admin/alt/", "regex_alternation"),
            ("/accounts/conv/", "converter_include"),
            ("/admin/leaf/", "converter_leaf"),
            ("/accounts/anywhere/x/", "unanchored"),
            ("/accounts/deep/p/", "path_converter"),
        ):
            with self.subTest(path=path):
                self.assertEqual(resolved_view_name(path, _mounts.__name__), name)

    def test_default_urlconf_keeps_its_names_and_adds_no_api_routes(self):
        names = oss_gated_view_names()
        for name in ("account_email", "admin:index", "admin:accounts_user_change", "account_logout"):
            with self.subTest(name=name):
                self.assertIn(name, names)
        # The API catch-alls are regexes with a diverging literal prefix.
        for name in ("share-board", "schema", "rest_logout", "rest_resend_email", "saml_acs"):
            with self.subTest(name=name):
                self.assertNotIn(name, names)
        self.assertEqual(check_pending_action_extra_exempt_views_not_oss_gated(None), [])

    @override_settings(PENDING_ACTION_EXTRA_GATED_PREFIXES=("/sso/",))
    def test_oss_routes_under_an_extra_prefix_are_named_and_extension_routes_are_not(self):
        names = oss_gated_view_names(_ext.__name__)
        self.assertIn("oss_under_sso", names)
        self.assertNotIn("ext_acs", names)
        # Under a built-in prefix the extension's own route still counts.
        self.assertIn("ext_under_accounts", names)
        self.assertIn("oss_under_accounts", names)

    @override_settings(PENDING_ACTION_EXTRA_GATED_PREFIXES=("/sso/",))
    def test_open_ended_literal_regex_under_an_extra_prefix_is_named(self):
        self.assertIn("oss_open_literal_ss", oss_gated_view_names(_ext.__name__))
        self.assertEqual(resolved_view_name("/sso/anything", _ext.__name__), "oss_open_literal_ss")

    @override_settings(PENDING_ACTION_EXTRA_GATED_PREFIXES=("/sso/", "no-slash", "/ext/"))
    def test_extra_gated_prefixes_are_the_well_formed_setting_entries(self):
        self.assertEqual(_valid_extra_gated_prefixes(), ("/sso/", "/ext/"))

    def test_without_the_extra_prefix_neither_sso_route_is_named(self):
        names = oss_gated_view_names(_ext.__name__)
        self.assertNotIn("oss_under_sso", names)
        self.assertNotIn("ext_acs", names)

    @override_settings(PENDING_ACTION_EXTRA_GATED_PREFIXES=("/sso/",))
    def test_a_root_without_an_extension_point_marks_every_route_oss(self):
        # This module's own URLconf declares no extension_urlpatterns.
        self.assertIn("saml_acs", oss_gated_view_names(__name__))

    @override_settings(
        PENDING_ACTION_EXTRA_GATED_PREFIXES=("/sso/",),
        PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("oss_under_sso", "ext_acs"),
    )
    def test_e007_and_the_runtime_filter_apply_to_extra_prefixes(self):
        with override_settings(ROOT_URLCONF=_ext.__name__):
            oss_gated_view_names.cache_clear()
            errors = check_pending_action_extra_exempt_views_not_oss_gated(None)
            self.assertEqual([e.id for e in errors], ["accounts.E007"])
            self.assertIn("oss_under_sso", errors[0].msg)
            self.assertFalse(is_pending_action_exempt_view("/sso/oss/"))
            self.assertTrue(is_pending_action_exempt_view("/sso/acs/"))

    @override_settings(PENDING_ACTION_EXTRA_EXEMPT_VIEWS=("regex_alternation", "int_leading"))
    def test_e007_reports_a_regex_mount_and_the_runtime_ignores_it(self):
        with override_settings(ROOT_URLCONF=_mounts.__name__):
            oss_gated_view_names.cache_clear()
            errors = check_pending_action_extra_exempt_views_not_oss_gated(None)
            self.assertEqual(len(errors), 1)
            self.assertIn("regex_alternation", errors[0].msg)
            self.assertFalse(is_pending_action_exempt_view("/accounts/alt/"))
            self.assertTrue(is_pending_action_exempt_view("/7/num/"))

    def test_cache_is_keyed_on_the_extra_prefixes(self):
        self.assertNotIn("oss_under_sso", oss_gated_view_names(_ext.__name__))
        with override_settings(PENDING_ACTION_EXTRA_GATED_PREFIXES=("/sso/",)):
            self.assertIn("oss_under_sso", oss_gated_view_names(_ext.__name__))


# ---------------------------------------------------------------------------
# System check: the middleware cannot be dropped silently
# ---------------------------------------------------------------------------


class _SubclassedGate(PendingAccountActionMiddleware):
    pass


class MiddlewareInstalledCheckTests(SimpleTestCase):
    def test_project_settings_pass(self):
        self.assertEqual(check_pending_action_middleware_installed(None), [])

    def test_dropping_the_middleware_fails(self):
        middleware = [m for m in settings.MIDDLEWARE if m != GATE]
        with override_settings(MIDDLEWARE=middleware):
            errors = check_pending_action_middleware_installed(None)
        self.assertEqual([e.id for e in errors], ["accounts.E005"])

    def test_placing_it_before_authentication_fails(self):
        middleware = [m for m in settings.MIDDLEWARE if m != GATE]
        middleware.insert(middleware.index(AUTH), GATE)
        with override_settings(MIDDLEWARE=middleware):
            errors = check_pending_action_middleware_installed(None)
        self.assertEqual([e.id for e in errors], ["accounts.E005"])

    def test_a_subclass_counts(self):
        dotted = f"{__name__}._SubclassedGate"
        middleware = [dotted if m == GATE else m for m in settings.MIDDLEWARE]
        with override_settings(MIDDLEWARE=middleware):
            self.assertEqual(check_pending_action_middleware_installed(None), [])

    def test_an_entry_that_fails_to_import_is_matched_by_name_only(self):
        """An ImproperlyConfigured middleware module yields a check result, not an exception."""
        from django.core.exceptions import ImproperlyConfigured
        from django.utils import module_loading

        real = module_loading.import_string

        def fake(dotted):
            if dotted == "broken.middleware.Thing":
                raise ImproperlyConfigured("broken")
            return real(dotted)

        middleware = ["broken.middleware.Thing", *settings.MIDDLEWARE]
        with override_settings(MIDDLEWARE=middleware), mock.patch.object(module_loading, "import_string", fake):
            self.assertEqual(check_pending_action_middleware_installed(None), [])
        without_gate = ["broken.middleware.Thing", *(m for m in settings.MIDDLEWARE if m != GATE)]
        with override_settings(MIDDLEWARE=without_gate), mock.patch.object(module_loading, "import_string", fake):
            errors = check_pending_action_middleware_installed(None)
        self.assertEqual([e.id for e in errors], ["accounts.E005"])

    def test_registered_with_django_checks(self):
        from django.core import checks

        self.assertIn(check_pending_action_middleware_installed, checks.registry.registry.get_checks())
        self.assertIn(check_pending_action_extra_exempt_views, checks.registry.registry.get_checks())


# ---------------------------------------------------------------------------
# SSO round trip
# ---------------------------------------------------------------------------


def _callback_request(user=None):
    request = RequestFactory().get("/accounts/github/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request.user = user or AnonymousUser()
    request._messages = FallbackStorage(request)
    return request


def _sociallogin(request, *, uid="gh-1", email="sso@example.com", process="login"):
    provider = get_adapter().get_provider(request, "github")
    account = SocialAccount(provider="github", uid=uid, extra_data={})
    sociallogin = SocialLogin(
        user=User(username="", email=email),
        account=account,
        email_addresses=[EmailAddress(email=email, verified=True, primary=True)],
    )
    sociallogin.provider = provider
    sociallogin.state = {"process": process}
    return sociallogin


@override_settings(
    FRONTEND_URL=FRONTEND,
    LOGIN_REDIRECT_URL=FRONTEND,
    SOCIALACCOUNT_PROVIDERS=PROVIDERS,
    STORAGES=PLAIN_STATIC,
)
class SsoRoundTripTests(TestCase):
    def setUp(self):
        cache.clear()

    def _pending(self, username, **flags):
        return User.objects.create_user(
            username=username, email=f"{username}@example.com", password=PASSWORD, **flags
        )

    def test_pending_user_completes_sign_in_and_lands_on_the_spa(self):
        for flags in FLAGS:
            with self.subTest(flags=flags):
                name = "sso" + next(iter(flags))[-8:]
                user = self._pending(name, **flags)
                uid = f"gh-{user.pk}"
                SocialAccount.objects.create(user=user, provider="github", uid=uid, extra_data={})
                request = _callback_request()
                # allauth's login stages read the request from its context,
                # as its own callback view sets it.
                with allauth_context.request_context(request):
                    response = complete_login(request, _sociallogin(request, uid=uid, email=user.email))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"].rstrip("/"), FRONTEND)
                self.assertEqual(str(request.user.pk), str(user.pk))
                # The landing page is outside the gate; anything under it
                # sends the signed-in user straight back to the SPA.
                client = Client()
                client.force_login(user)
                self.assertEqual(client.get("/accounts/email/")["Location"], FRONTEND)

    def test_pending_user_cannot_connect_a_provider(self):
        for flags in FLAGS:
            with self.subTest(flags=flags):
                name = "conn" + next(iter(flags))[-8:]
                user = self._pending(name, **flags)
                request = _callback_request(user)
                with self.assertRaises(ImmediateHttpResponse) as ctx:
                    pre_social_login(request, _sociallogin(request, uid=f"new-{user.pk}", process="connect"))
                self.assertEqual(ctx.exception.response["Location"], FRONTEND)
                self.assertFalse(SocialAccount.objects.filter(user=user).exists())

    def test_pending_user_connect_through_complete_login_links_nothing(self):
        user = self._pending("connfull", must_change_password=True)
        request = _callback_request(user)
        response = complete_login(request, _sociallogin(request, uid="gh-full", process="connect"))
        self.assertEqual(response["Location"], FRONTEND)
        self.assertFalse(SocialAccount.objects.filter(uid="gh-full").exists())

    def test_clear_user_still_connects(self):
        user = self._pending("clear")
        request = _callback_request(user)
        response = complete_login(request, _sociallogin(request, uid="gh-clear", process="connect"))
        self.assertEqual(urlsplit(response["Location"]).path, "/settings")
        self.assertTrue(SocialAccount.objects.filter(user=user, uid="gh-clear").exists())


# ---------------------------------------------------------------------------
# Ordering with the maintenance and demo middleware
# ---------------------------------------------------------------------------


@override_settings(FRONTEND_URL=FRONTEND, STORAGES=PLAIN_STATIC)
class MiddlewareOrderingTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _client(self, **fields):
        user = User.objects.create_user(username="order", email="order@example.com", password=PASSWORD, **fields)
        client = Client()
        client.force_login(user)
        return client

    def test_order_in_settings(self):
        middleware = list(settings.MIDDLEWARE)
        gate = middleware.index(GATE)
        self.assertGreater(gate, middleware.index(AUTH))
        self.assertGreater(gate, middleware.index("visiban.middleware.MaintenanceModeMiddleware"))
        self.assertGreater(gate, middleware.index("visiban.middleware.DemoModeMiddleware"))

    def test_maintenance_answers_first_for_a_non_admin_write(self):
        client = self._client(must_change_password=True)
        setting = SiteSetting.get()
        setting.maintenance_mode = True
        setting.save(update_fields=["maintenance_mode"])
        cache.clear()
        r = client.post("/accounts/email/", {"action_add": "", "email": "m@example.com"})
        self.assertEqual(r.status_code, 503)
        # A read is not a maintenance concern; the gate still redirects it.
        self.assertEqual(client.get("/accounts/email/")["Location"], FRONTEND)

    def test_gate_applies_to_a_site_admin_during_maintenance(self):
        client = self._client(must_change_password=True, is_site_admin=True, is_staff=True)
        setting = SiteSetting.get()
        setting.maintenance_mode = True
        setting.save(update_fields=["maintenance_mode"])
        cache.clear()
        r = client.post("/admin/", {})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.content.decode(), PENDING_ACTION_REFUSAL)

    @override_settings(DEMO_MODE=True)
    def test_demo_fence_still_refuses_an_exempt_write(self):
        """An exemption from the forced-change gate is not an exemption from the demo fence."""
        client = self._client(must_change_password=True)
        r = client.post("/accounts/password/reset/", {"email": "order@example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], DEMO_READ_ONLY_CODE)
        self.assertEqual(client.get("/accounts/email/")["Location"], FRONTEND)
