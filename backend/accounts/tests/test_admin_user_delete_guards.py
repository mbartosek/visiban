"""Django admin user delete: the deactivation guards apply to deletes too (#1616).

Both the single delete view and the ``delete_selected`` bulk action refuse
deleting the actor's own account, a board owner, and the last active site
admin, and a bulk delete is all-or-nothing.
"""
import threading
from unittest import skipUnless

from django.contrib.auth.models import Permission
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from accounts.models import User
from boards.models import Board

PW = "pw123456!x"

_PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


def _staff(name, *, site_admin=False, superuser=False):
    user = User.objects.create_user(username=name, email=f"{name}@example.com", password=PW)
    user.is_staff = True
    user.is_site_admin = site_admin
    user.is_superuser = superuser
    user.save()
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="accounts",
            content_type__model="user",
            codename__in=["view_user", "change_user", "delete_user"],
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
