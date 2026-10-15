"""Cleanup of API auth tokens held by inactive accounts (#1674)."""
import importlib

from django.apps import apps
from django.test import TestCase
from rest_framework.authtoken.models import Token

from accounts.models import User

_migration = importlib.import_module("accounts.migrations.0039_remove_inactive_user_auth_tokens")


class InactiveUserAuthTokenMigrationTests(TestCase):
    def setUp(self):
        self.inactive = User.objects.create_user(username="inactive_1674", password="pw123456!x")
        self.active = User.objects.create_user(username="active_1674", password="pw123456!x")
        Token.objects.create(user=self.inactive)
        Token.objects.create(user=self.active)
        User.objects.filter(pk=self.inactive.pk).update(is_active=False)

    def test_inactive_user_token_removed_and_active_kept(self):
        _migration.remove_inactive_user_auth_tokens(apps, None)
        self.assertFalse(Token.objects.filter(user=self.inactive).exists())
        self.assertTrue(Token.objects.filter(user=self.active).exists())

    def test_idempotent(self):
        _migration.remove_inactive_user_auth_tokens(apps, None)
        _migration.remove_inactive_user_auth_tokens(apps, None)
        self.assertEqual(Token.objects.count(), 1)
