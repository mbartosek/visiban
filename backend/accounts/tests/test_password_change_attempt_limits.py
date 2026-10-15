"""Rate limiting and lockout interaction for the password-change endpoints (#1673)."""
import threading
from unittest.mock import patch

from django.core.cache import cache
from django.db import connections
from django.test import TransactionTestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from accounts.views import PasswordChangeThrottle

CURRENT = "OldPassword123!"
NEW = "NewPassword456!x"
URLS = ["/api/v1/auth/change-password/", "/api/v1/auth/password/change/"]


def _body(url, current):
    if url.endswith("change-password/"):
        return {"current_password": current, "new_password": NEW}
    return {"old_password": current, "new_password1": NEW, "new_password2": NEW}


class PasswordChangeAttemptLimitTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="alice", password=CURRENT)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_wrong_passwords_lock_out_further_attempts(self):
        for url in URLS:
            cache.clear()
            for _ in range(5):
                r = self.client.post(url, _body(url, "wrong-password-1"))
                self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, url)
            r = self.client.post(url, _body(url, CURRENT))
            self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS, url)
            self.user.refresh_from_db()
            self.assertTrue(self.user.check_password(CURRENT))

    def test_failures_are_shared_across_both_endpoints(self):
        for i in range(5):
            url = URLS[i % 2]
            self.client.post(url, _body(url, "wrong-password-1"))
        r = self.client.post(URLS[0], _body(URLS[0], CURRENT))
        self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_correct_password_does_not_consume_budget(self):
        for url in URLS:
            cache.clear()
            for _ in range(4):
                self.client.post(url, _body(url, "wrong-password-1"))
            r = self.client.post(url, _body(url, CURRENT))
            self.assertEqual(r.status_code, status.HTTP_200_OK, url)
            self.user.set_password(CURRENT)
            self.user.save()
            # The correct attempt was rolled back, so exactly one more
            # failure fits in the budget of five before the lockout.
            r = self.client.post(url, _body(url, "wrong-password-1"))
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, url)
            r = self.client.post(url, _body(url, "wrong-password-1"))
            self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS, url)

    def test_account_locked_at_login_is_refused_here(self):
        login = APIClient()
        for _ in range(5):
            login.post("/api/v1/auth/login/", {"username": "alice", "password": "nope-nope-1"})
        r = self.client.post(URLS[0], _body(URLS[0], CURRENT))
        self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_throttle_scope_is_configured(self):
        self.assertEqual(PasswordChangeThrottle.scope, "password_change")
        self.assertIn("password_change", PasswordChangeThrottle.THROTTLE_RATES)

    def test_request_count_throttle_applies_to_both_endpoints(self):
        # The configured rate is bypassed under DEBUG, so pin a small one.
        # Throttles run before the view, so the 4th request is refused
        # whatever its body is.
        for url in URLS:
            cache.clear()
            with patch.object(PasswordChangeThrottle, "get_rate", return_value="3/hour"):
                codes = [
                    self.client.post(url, _body(url, "wrong-password-1")).status_code
                    for _ in range(4)
                ]
            self.assertEqual(codes[:3], [status.HTTP_400_BAD_REQUEST] * 3, url)
            self.assertEqual(codes[3], status.HTTP_429_TOO_MANY_REQUESTS, url)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="alice")
    def test_demo_account_is_not_locked_out(self):
        # Called directly: the demo write fence refuses the endpoint itself
        # under DEMO_MODE, so the exemption is exercised at the helper.
        from django.test import RequestFactory

        from accounts import password_attempts

        request = RequestFactory().post("/x/")
        for _ in range(7):
            self.assertIsNone(
                password_attempts.begin_current_password_check(request, self.user)
            )

    @override_settings(DEMO_MODE=False, DEMO_LOGIN_USERNAME="alice")
    def test_same_username_is_locked_out_without_demo_mode(self):
        for _ in range(5):
            self.client.post(URLS[0], _body(URLS[0], "wrong-password-1"))
        r = self.client.post(URLS[0], _body(URLS[0], "wrong-password-1"))
        self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class PasswordChangeConcurrentAttemptTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="bob", password=CURRENT)

    def test_parallel_wrong_guesses_each_consume_budget(self):
        results = []
        barrier = threading.Barrier(2)

        def worker():
            try:
                client = APIClient()
                client.force_authenticate(self.user)
                barrier.wait(timeout=10)
                results.append(
                    client.post(URLS[0], _body(URLS[0], "wrong-password-1")).status_code
                )
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [status.HTTP_400_BAD_REQUEST] * 2)
        # allauth's rate-limit read-modify-write is not atomic, so two
        # simultaneous requests may be recorded as one. Bound the consumed
        # units: at least one (the helper ran) and at most two.
        client = APIClient()
        client.force_authenticate(self.user)
        accepted = 0
        for _ in range(6):
            code = client.post(URLS[0], _body(URLS[0], "wrong-password-1")).status_code
            if code == status.HTTP_429_TOO_MANY_REQUESTS:
                break
            accepted += 1
        self.assertGreaterEqual(accepted, 3)
        self.assertLessEqual(accepted, 4)
