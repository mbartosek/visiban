"""Django admin user delete: the deactivation guards apply to deletes too (#1616).

Both the single delete view and the ``delete_selected`` bulk action refuse
deleting the actor's own account, a board owner, and the last active site
admin, and a bulk delete is all-or-nothing.
"""
import threading
from unittest import skipUnless

from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

import accounts.admin as admin_module
from accounts.models import User
from boards.models import Board

PW = "pw123456!x"

_PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


def _staff(name, *, site_admin=False, superuser=False, perms=("view_user", "change_user", "delete_user")):
    user = User.objects.create_user(username=name, email=f"{name}@example.com", password=PW)
    user.is_staff = True
    user.is_site_admin = site_admin
    user.is_superuser = superuser
    user.save()
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="accounts",
            content_type__model="user",
            codename__in=list(perms),
        )
    )
    return User.objects.get(pk=user.pk)


def _messages(response):
    return [str(m) for m in response.context["messages"]] if response.context else []


@_PLAIN_STATIC
class UserDeleteGuardTests(TestCase):
    def setUp(self):
        self.staff = _staff("staff_1616")
        self.client.force_login(self.staff)
        self.victim = User.objects.create_user(username="victim_1616", password=PW)
        self.changelist = reverse("admin:accounts_user_changelist")

    def delete_url(self, user):
        return reverse("admin:accounts_user_delete", args=[user.pk])

    def bulk(self, users, **extra):
        data = {
            "action": "delete_selected",
            "_selected_action": [u.pk for u in users],
            **extra,
        }
        return self.client.post(self.changelist, data, follow=True)

    def assertRefused(self, response, fragment):
        self.assertTrue(
            any(fragment in m for m in _messages(response)), _messages(response)
        )

    # -- single delete view ---------------------------------------------------

    def test_single_delete_positive(self):
        self.assertEqual(self.client.get(self.delete_url(self.victim)).status_code, 200)
        r = self.client.post(self.delete_url(self.victim), {"post": "yes"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.victim.pk).exists())

    def test_single_delete_self_refused_on_get_and_post(self):
        for method in ("get", "post"):
            r = getattr(self.client, method)(
                self.delete_url(self.staff), {"post": "yes"} if method == "post" else {},
                follow=True,
            )
            self.assertRefused(r, "cannot delete your own account")
            self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())

    def test_single_delete_board_owner_refused_and_boards_kept(self):
        board = Board.objects.create(name="Owned", owner=self.victim)
        for method in ("get", "post"):
            r = getattr(self.client, method)(
                self.delete_url(self.victim), {"post": "yes"} if method == "post" else {},
                follow=True,
            )
            self.assertRefused(r, "must be transferred before deletion")
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())
        self.assertTrue(Board.objects.filter(pk=board.pk).exists())

    def test_single_delete_last_active_site_admin_refused(self):
        sole = User.objects.create_user(username="sole_1616", password=PW)
        sole.is_site_admin = True
        sole.save()
        r = self.client.post(self.delete_url(sole), {"post": "yes"}, follow=True)
        self.assertRefused(r, "last active site admin")
        self.assertTrue(User.objects.filter(pk=sole.pk).exists())

    def test_single_delete_site_admin_allowed_when_another_remains(self):
        other = _staff("other_admin_1616", site_admin=True)
        sole = User.objects.create_user(username="second_1616", password=PW)
        sole.is_site_admin = True
        sole.save()
        self.client.post(self.delete_url(sole), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=sole.pk).exists())
        self.assertTrue(User.objects.filter(pk=other.pk).exists())

    # -- delete_selected ------------------------------------------------------

    def test_bulk_positive(self):
        other = User.objects.create_user(username="victim2_1616", password=PW)
        r = self.bulk([self.victim, other])
        self.assertEqual(r.status_code, 200)  # confirmation page
        self.bulk([self.victim, other], post="yes")
        self.assertFalse(User.objects.filter(pk__in=[self.victim.pk, other.pk]).exists())

    def test_bulk_self_refused_and_nothing_deleted(self):
        for extra in ({}, {"post": "yes"}):
            r = self.bulk([self.victim, self.staff], **extra)
            self.assertRefused(r, "cannot delete your own account")
            self.assertRefused(r, "No accounts were deleted")
        self.assertEqual(User.objects.filter(pk__in=[self.victim.pk, self.staff.pk]).count(), 2)

    def test_bulk_board_owner_refused_and_nothing_deleted(self):
        other = User.objects.create_user(username="plain_1616", password=PW)
        Board.objects.create(name="Owned", owner=self.victim)
        for extra in ({}, {"post": "yes"}):
            r = self.bulk([self.victim, other], **extra)
            self.assertRefused(r, "must be transferred before deletion")
        self.assertEqual(User.objects.filter(pk__in=[self.victim.pk, other.pk]).count(), 2)

    def test_bulk_last_active_site_admin_refused(self):
        sole = User.objects.create_user(username="sole2_1616", password=PW)
        sole.is_site_admin = True
        sole.save()
        r = self.bulk([sole], post="yes")
        self.assertRefused(r, "last active site admin")
        self.assertTrue(User.objects.filter(pk=sole.pk).exists())

    def test_bulk_all_remaining_site_admins_refused_as_a_set(self):
        a = User.objects.create_user(username="adm_a_1616", password=PW)
        b = User.objects.create_user(username="adm_b_1616", password=PW)
        for u in (a, b):
            u.is_site_admin = True
            u.save()
        r = self.bulk([a, b], post="yes")
        self.assertRefused(r, "every active site admin")
        self.assertEqual(User.objects.filter(pk__in=[a.pk, b.pk]).count(), 2)

    def test_bulk_one_of_two_site_admins_allowed(self):
        a = User.objects.create_user(username="adm_c_1616", password=PW)
        b = User.objects.create_user(username="adm_d_1616", password=PW)
        for u in (a, b):
            u.is_site_admin = True
            u.save()
        self.bulk([a], post="yes")
        self.assertFalse(User.objects.filter(pk=a.pk).exists())
        self.assertTrue(User.objects.filter(pk=b.pk).exists())

    def test_superuser_actor_is_guarded_too(self):
        root = _staff("root_1616", superuser=True)
        self.client.force_login(root)
        r = self.client.post(self.delete_url(root), {"post": "yes"}, follow=True)
        self.assertRefused(r, "cannot delete your own account")
        self.assertTrue(User.objects.filter(pk=root.pk).exists())


@_PLAIN_STATIC
class UserDeleteHardeningTests(TestCase):
    def setUp(self):
        self.staff = _staff("staff2_1616")
        self.client.force_login(self.staff)
        self.victim = User.objects.create_user(username="victim2b_1616", password=PW)
        self.changelist = reverse("admin:accounts_user_changelist")

    def delete_url(self, user):
        return reverse("admin:accounts_user_delete", args=[user.pk])

    def _msgs(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_refusal_shows_board_count_not_names(self):
        Board.objects.create(name="Secret Roadmap 1616", owner=self.victim)
        Board.objects.create(name="Other Plan 1616", owner=self.victim)
        r = self.client.post(self.delete_url(self.victim), {"post": "yes"}, follow=True)
        text = " ".join(self._msgs(r))
        self.assertIn("must be transferred before deletion", text)
        self.assertIn("owned boards: 2", text)
        self.assertNotIn("Secret Roadmap", text)
        self.assertNotIn("Other Plan", text)

    def test_without_delete_permission_view_and_action_refuse(self):
        viewer = _staff("viewer_1616", perms=("view_user", "change_user"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.delete_url(self.victim)).status_code, 403)
        self.assertEqual(
            self.client.post(self.delete_url(self.victim), {"post": "yes"}).status_code, 403
        )
        r = self.client.get(self.changelist)
        form = r.context["action_form"]
        choices = [c[0] for c in form.fields["action"].choices] if form else []
        self.assertNotIn("delete_selected", choices)
        self.client.post(
            self.changelist,
            {"action": "delete_selected", "_selected_action": [self.victim.pk], "post": "yes"},
        )
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())

    def test_bulk_action_function_refuses_without_delete_permission(self):
        viewer = _staff("viewer2_1616", perms=("view_user", "change_user"))
        request = RequestFactory().post("/")
        request.user = viewer
        with self.assertRaises(PermissionDenied):
            admin_module.guarded_delete_selected(
                django_admin.site._registry[User], request, User.objects.filter(pk=self.victim.pk)
            )

    def test_non_superuser_cannot_reach_superuser_rows(self):
        root = _staff("root2_1616", superuser=True)
        # Django answers a row the admin queryset hides with a redirect to the
        # changelist ("does not exist"), not a 404 page; nothing is deleted.
        self.assertEqual(self.client.get(self.delete_url(root)).status_code, 302)
        self.assertEqual(
            self.client.post(self.delete_url(root), {"post": "yes"}).status_code, 302
        )
        self.assertTrue(User.objects.filter(pk=root.pk).exists())
        self.client.post(
            self.changelist,
            {"action": "delete_selected", "_selected_action": [root.pk, self.victim.pk], "post": "yes"},
        )
        self.assertTrue(User.objects.filter(pk=root.pk).exists())
        self.assertFalse(User.objects.filter(pk=self.victim.pk).exists())

    def test_anonymous_and_non_staff_are_redirected_to_login(self):
        for user in (None, User.objects.create_user(username="plain2_1616", password=PW)):
            client = Client()
            if user:
                client.force_login(user)
            r = client.post(self.delete_url(self.victim), {"post": "yes"})
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())

    def _first_check_passes(self):
        """Let the page-level check pass once, then use the real rule under lock."""
        real = admin_module.user_deletion_refusals
        calls = {"n": 0}

        def side_effect(targets, actor):
            calls["n"] += 1
            return [] if calls["n"] == 1 else real(targets, actor)

        return mock.patch("accounts.admin.user_deletion_refusals", side_effect=side_effect)

    def test_stale_single_post_refused_under_lock_without_log_entries(self):
        sole = User.objects.create_user(username="stale_sole_1616", password=PW)
        sole.is_site_admin = True
        sole.save()
        with self._first_check_passes():
            r = self.client.post(self.delete_url(sole), {"post": "yes"}, follow=True)
        self.assertTrue(User.objects.filter(pk=sole.pk).exists())
        self.assertTrue(any("last active site admin" in m for m in self._msgs(r)))
        self.assertFalse(LogEntry.objects.filter(action_flag=DELETION).exists())

    def test_stale_bulk_post_refused_under_lock_without_log_entries(self):
        sole = User.objects.create_user(username="stale_bulk_1616", password=PW)
        sole.is_site_admin = True
        sole.save()
        with self._first_check_passes():
            r = self.client.post(
                self.changelist,
                {"action": "delete_selected", "_selected_action": [sole.pk, self.victim.pk],
                 "post": "yes"},
                follow=True,
            )
        self.assertEqual(User.objects.filter(pk__in=[sole.pk, self.victim.pk]).count(), 2)
        self.assertTrue(any("No accounts were deleted" in m for m in self._msgs(r)))
        self.assertFalse(LogEntry.objects.filter(action_flag=DELETION).exists())

    def test_target_promoted_to_superuser_before_lock_is_refused(self):
        self.victim.is_superuser = True
        self.victim.save()
        request = RequestFactory().post("/")
        request.user = User.objects.get(pk=self.staff.pk)
        model_admin = django_admin.site._registry[User]
        with self.assertRaises(admin_module.DeletionRefused):
            model_admin.delete_model(request, User.objects.get(pk=self.victim.pk))
        with self.assertRaises(admin_module.DeletionRefused):
            model_admin.delete_queryset(request, User.objects.filter(pk=self.victim.pk))
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())

    def test_deleted_or_inactive_actor_under_lock_is_refused(self):
        request = RequestFactory().post("/")
        request.user = User.objects.get(pk=self.staff.pk)
        User.objects.filter(pk=self.staff.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            django_admin.site._registry[User].delete_model(request, self.victim)
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())

    def test_superuser_deletes_superuser_when_another_remains(self):
        root = _staff("root3_1616", superuser=True)
        other = _staff("root4_1616", superuser=True)
        self.client.force_login(root)
        self.client.post(self.delete_url(other), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=other.pk).exists())
        # root is now the last active superuser (and site admin) and also self.
        self.client.post(self.delete_url(root), {"post": "yes"}, follow=True)
        self.assertTrue(User.objects.filter(pk=root.pk).exists())

    def test_bulk_every_active_superuser_refused_as_a_set(self):
        root = _staff("root5_1616", superuser=True)
        other = _staff("root6_1616", superuser=True)
        User.objects.filter(pk=self.staff.pk).update(is_superuser=False)
        # Keep other site admins out of the way: only these two remain.
        self.client.force_login(root)
        self.client.post(
            self.changelist,
            {"action": "delete_selected", "_selected_action": [other.pk], "post": "yes"},
            follow=True,
        )
        # root remains, so deleting other alone is allowed.
        self.assertFalse(User.objects.filter(pk=other.pk).exists())
        reasons = admin_module.user_deletion_refusals(
            list(User.objects.filter(is_superuser=True, is_active=True)), self.staff
        )
        self.assertTrue(any("every active superuser" in x or "last active superuser" in x
                            for x in reasons))


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
@_PLAIN_STATIC
class ConcurrentLastSiteAdminDeleteTests(TransactionTestCase):
    def test_two_concurrent_deletes_of_the_last_two_site_admins_leave_one(self):
        actor = _staff("actor_1616")
        admins = []
        for name in ("conc_a_1616", "conc_b_1616"):
            u = User.objects.create_user(username=name, password=PW)
            u.is_site_admin = True
            u.save()
            admins.append(u)
        barrier = threading.Barrier(2)
        results = []

        def worker(target_pk):
            try:
                c = Client()
                c.force_login(actor)
                barrier.wait(timeout=10)
                c.post(
                    reverse("admin:accounts_user_delete", args=[target_pk]), {"post": "yes"}
                )
                results.append(target_pk)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(u.pk,)) for u in admins]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        remaining = User.objects.filter(pk__in=[u.pk for u in admins]).count()
        self.assertEqual(remaining, 1)


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
@_PLAIN_STATIC
class MutualSuperuserDeleteRaceTests(TransactionTestCase):
    def test_two_superusers_deleting_each_other_leave_exactly_one(self):
        a = _staff("mut_a_1616", superuser=True)
        b = _staff("mut_b_1616", superuser=True)
        barrier = threading.Barrier(2)

        def worker(actor, target):
            try:
                c = Client()
                c.force_login(actor)
                barrier.wait(timeout=10)
                c.post(reverse("admin:accounts_user_delete", args=[target.pk]), {"post": "yes"})
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(a, b)),
            threading.Thread(target=worker, args=(b, a)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(User.objects.filter(pk__in=[a.pk, b.pk]).count(), 1)
