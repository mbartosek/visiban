# Django Admin Panel

Django's built-in admin — for direct database record editing, not day-to-day
administration — lives at `/admin/` (**with** a trailing slash) on the backend
server (port 8000 in development).

Why it matters: this is a different page from the React **site-admin panel**
at `/admin` (**no** trailing slash — see [Admin Panel](admin-panel.md)), which
is what most day-to-day tasks (users, registration mode, maintenance mode)
should go through. The two share a URL prefix by coincidence of routing, not
by design — Nginx and the SPA router tell them apart by the trailing slash.

## Access

Log in with any user that has `is_staff = True` or `is_superuser = True`. The site admin account created by `ensure_site_admin` has `is_staff` and `is_superuser` set, so it can sign in here once it has changed the generated password (see below). To give another account Django admin access, set `is_staff` on it:

```bash
python manage.py shell -c "
from accounts.models import User
u = User.objects.get(username='someone')
u.is_staff = True
u.save()
"
```

An account with a forced password or username change pending (`must_change_password` or `must_change_username`) cannot use the Django admin until it completes the change in the web app: `/admin/` redirects it to the app and refuses form submissions. Only **Log out** stays available. The account created by `ensure_site_admin` starts with `must_change_password` set, so it changes its password in the web app before it can use the Django admin.

## Passwords

- **Your own password:** the admin's **Change password** link opens the web app, where you change it like any other user.
- **Another user's password** (the user's **Password** form at `/admin/accounts/user/<id>/password/`): saving a new password, or disabling password sign-in, deletes that user's Personal Access Tokens and API token and ends their sessions. If a new password was set, the user must change it the next time they sign in.
- **Adding a user:** a user created here with a password must choose their own password the first time they sign in, the same as one created from the site-admin panel. A user created with password sign-in disabled is not asked to create one.

## Deactivating users

Clearing **Active** on a user deletes their Personal Access Tokens and revokes their pending invite links, the same as deactivating them from the site-admin panel.

## What you can manage

- **Users** — view/edit user accounts, activate or deactivate accounts, and (superusers only) set `is_staff`, `is_superuser`, groups and permissions. The form does not carry `is_site_admin` or `must_change_password`; use the site-admin panel or `set_site_admin` for those. Removing `is_superuser` also removes the account's site-admin and all-content access; site admin can then be granted again explicitly.
  - For staff who are not superusers, `is_staff`, `is_superuser`, groups and permissions are read-only, `email`, `username` and `is_active` are read-only on staff, superuser and site-admin accounts other than their own, superuser accounts are not listed, and the **Password** form is refused for those accounts. Changing or deleting another staff, superuser or site-admin account is left to superusers.
  - For every user, superusers included, the form applies the same guards as the site-admin panel: you cannot deactivate your own account, a user who owns boards without transferring them, or the last active site admin, and you cannot remove your own superuser status, the last active superuser's status, or the last active site admin's access. The shell and management commands are the only routes without these guards.
  - Deleting a user, from the user's delete page or the **Delete selected users** action, applies the same guards: you cannot delete your own account, a user who owns boards or groups (transfer them first), the last active site admin, or the last active superuser; staff accounts that are not superusers cannot delete another staff, superuser or site-admin account. A bulk delete is all-or-nothing: if any selected user is refused, none are deleted and each reason is shown.
  - If the account changed after you opened its form, the save is refused with a message asking you to reload the page and make your changes again.
- **Groups** (Django auth permission groups) — visible and editable by superusers only; staff accounts do not see this entry even when granted the group permissions
- **Site settings** — instance-wide configuration (registration mode, file uploads, [maintenance mode](maintenance-mode.md)); clicking the entry goes directly to the settings form — there is always exactly one row
- **Boards, columns, swimlanes** — direct database access for debugging
- **Board memberships** — view and correct membership records
- **Email addresses, social accounts, social tokens, social apps, API tokens and Sites** — available to superusers only. Staff accounts do not see these entries.

## Production note

**Restricted to loopback by default** — both the bundled Nginx config and
`AdminIPRestrictionMiddleware` block any client IP that isn't `127.0.0.1` or
`::1`, unless you widen the allowlist. See [Secret Rotation → Admin interface
access](secret-rotation.md#admin-interface-access) for the Compose and Helm
steps, and why they differ.
