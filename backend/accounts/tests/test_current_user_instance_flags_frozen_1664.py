"""Freeze guard for the instance-level flags on ``/auth/user/`` (#1664).

``CurrentUserSerializer`` is the 1.0+ identity object. 1.2 let instance-wide
flags (``git_lens_enabled``, ``maintenance_*``, ``demo_*``) accumulate on it, and
every one is now a permanent field. These pins make the next such field a
deliberate decision: new instance-level flags belong on ``SiteConfig``
(``GET /auth/site-config/``) or a capabilities payload, not here.

``demo_mode`` / ``demo_next_reset_at`` also live on SiteConfig. That duplication
is intentional (the SPA bootstraps from ``/auth/user/`` post-login and from
``site-config`` pre-login); the last test keeps the two shapes from drifting.
"""
from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from accounts.serializers import CurrentUserSerializer, UserSerializer

GUIDANCE = (
    "New instance-level flags go on SiteConfig - see "
    "docs/api/authentication.md#frozen-instance-flags-on-authuser; adding a "
    "per-user field requires updating this pin deliberately."
)

# Instance-wide (not per-user) flags already on the identity object. Frozen:
# never remove or rename (1.0 contract), never add to this list.
# uploads_enabled predates 1.2; git_lens_enabled's freeze status is tracked in #1666.
FROZEN_INSTANCE_FLAGS = {
    "uploads_enabled",
    "git_lens_enabled",
    "maintenance_mode",
    "maintenance_message",
    "demo_mode",
    "demo_next_reset_at",
}

# Per-user additions on CurrentUserSerializer (state of this account, not the instance).
PER_USER_EXTRAS = {"pending_email", "pending_connect_provider", "pending_connect_identity"}

PINNED_FIELDS = {
    "id", "username", "email", "first_name", "last_name", "avatar_url", "display_name",
    "is_site_admin", "can_access_all_content", "must_change_password", "must_change_username",
    "has_usable_password", "timezone", "date_format", "time_format", "number_locale",
    "close_editor_on_enter", "has_completed_tour", "theme",
    "notif_card_assigned", "notif_mentioned", "notif_due_soon", "notif_card_moved",
    "notif_comment_added", "notif_board_invite", "notif_stale",
    "email_notif_card_assigned", "email_notif_mentioned", "email_notif_due_soon",
    "email_notif_card_moved", "email_notif_comment_added", "default_board_id",
} | FROZEN_INSTANCE_FLAGS | PER_USER_EXTRAS


class CurrentUserFieldsFrozenTests(TestCase):
    def test_serializer_field_set_is_pinned(self):
        actual = set(CurrentUserSerializer.Meta.fields)
        self.assertEqual(
            actual,
            PINNED_FIELDS,
            f"{GUIDANCE} Added: {sorted(actual - PINNED_FIELDS)}; removed: {sorted(PINNED_FIELDS - actual)}.",
        )

    def test_instance_flag_subset_is_pinned(self):
        extras = set(CurrentUserSerializer.Meta.fields) - set(UserSerializer.Meta.fields)
        instance_flags = (extras - PER_USER_EXTRAS) | (
            set(UserSerializer.Meta.fields) & FROZEN_INSTANCE_FLAGS
        )
        self.assertEqual(
            instance_flags,
            FROZEN_INSTANCE_FLAGS,
            f"{GUIDANCE} Added: {sorted(instance_flags - FROZEN_INSTANCE_FLAGS)}.",
        )

    def test_auth_user_response_keys_are_pinned(self):
        user = User.objects.create_user(username="frozen-1664", password="pw-1664-Abcdef!")
        client = APIClient()
        client.force_authenticate(user)
        keys = set(client.get("/api/v1/auth/user/").json())
        self.assertEqual(keys, PINNED_FIELDS, f"{GUIDANCE} Extra keys: {sorted(keys - PINNED_FIELDS)}.")


class DemoFieldsMatchSiteConfigTests(TestCase):
    """The intentionally duplicated ``demo_*`` fields must keep one shape."""

    def _both(self):
        user = User.objects.create_user(username="dup-1664", password="pw-1664-Abcdef!")
        client = APIClient()
        client.force_authenticate(user)
        me = client.get("/api/v1/auth/user/").json()
        cfg = APIClient().get("/api/v1/auth/site-config/").json()
        return me, cfg

    def test_demo_off_same_values_and_types(self):
        me, cfg = self._both()
        for key in ("demo_mode", "demo_next_reset_at"):
            self.assertEqual(me[key], cfg[key], key)
            self.assertIs(type(me[key]), type(cfg[key]), key)

    @override_settings(DEMO_MODE=True, DEMO_RESET_SCHEDULE="0 * * * *")
    def test_demo_on_same_values_and_types(self):
        me, cfg = self._both()
        self.assertIs(me["demo_mode"], True)
        self.assertIsInstance(me["demo_next_reset_at"], str)
        for key in ("demo_mode", "demo_next_reset_at"):
            self.assertIs(type(me[key]), type(cfg[key]), key)
