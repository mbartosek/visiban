"""Non-object / wrong-typed JSON bodies are 400, never 500, on auth endpoints (#1669).

The "valid request unchanged" controls passed before the fix; they are
regression guards. The malformed-body assertions are the fix.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import SiteSetting, User, invalidate_registration_mode_cache

BAD_BODIES = ([1, 2], 5, "text", True)


class AuthEndpointMalformedBodyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass123secure!")
        self.client = APIClient(raise_request_exception=False)
        self.client.force_authenticate(self.user)

    def test_change_password_malformed(self):
        url = "/api/v1/auth/change-password/"
        for bad in BAD_BODIES:
            self.assertEqual(self.client.post(url, bad, format="json").status_code, 400, repr(bad))
        for bad in (5, ["x"], {"a": 1}):
            r = self.client.post(url, {"current_password": bad, "new_password": bad}, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_choose_username_malformed(self):
        url = "/api/v1/auth/choose-username/"
        for bad in BAD_BODIES:
            r = self.client.post(url, bad, format="json")
            self.assertTrue(400 <= r.status_code < 500, f"{bad!r}: {r.status_code}")
        for bad in (5, ["x"], {"a": 1}):
            r = self.client.post(url, {"username": bad}, format="json")
            self.assertTrue(400 <= r.status_code < 500, f"{bad!r}: {r.status_code}")

    def test_pat_create_malformed(self):
        url = "/api/v1/auth/tokens/"
        for bad in BAD_BODIES:
            self.assertEqual(self.client.post(url, bad, format="json").status_code, 400, repr(bad))
        for bad in (5, ["x"], {"a": 1}):
            self.assertEqual(self.client.post(url, {"name": bad}, format="json").status_code, 400, repr(bad))
            r = self.client.post(url, {"name": "ok", "expires_at": bad}, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_pat_create_valid_unchanged(self):
        r = self.client.post("/api/v1/auth/tokens/", {"name": "ci"}, format="json")
        self.assertEqual(r.status_code, 201)

    def test_username_email_throttle_tolerates_non_object_body(self):
        # allow_request already catches AttributeError; the serializer rejects it.
        r = self.client.patch("/api/v1/auth/user/", [1, 2], format="json")
        self.assertTrue(400 <= r.status_code < 500, r.status_code)


class InviteRegistrationMalformedBodyTests(TestCase):
    def setUp(self):
        s = SiteSetting.get()
        s.registration_mode = SiteSetting.RegistrationMode.INVITE_ONLY
        s.save()
        invalidate_registration_mode_cache()
        self.addCleanup(invalidate_registration_mode_cache)
        self.client = APIClient(raise_request_exception=False)

    def test_non_object_body_same_as_missing_token(self):
        url = "/api/v1/auth/registration/"
        missing = self.client.post(url, {}, format="json")
        self.assertEqual(missing.status_code, 400)
        for bad in BAD_BODIES:
            r = self.client.post(url, bad, format="json")
            self.assertEqual(r.status_code, 400, repr(bad))
            self.assertEqual(r.json(), missing.json(), repr(bad))
