"""Remove attachment files that no ``CardAttachment`` row points at (#1817).

Deleting a card, column, swimlane or board removes the attachment rows, and a
``post_delete`` receiver removes their files after commit. Files can still be
orphaned: every delete before that receiver shipped left its files behind, a
storage outage at delete time loses the removal, and an upload whose request
rolled back leaves a file with no row. This command is the repair path.

Safety rails:

- **Dry run by default.** Without ``--yes`` it only reports how many orphans
  it found.
- **Only ``attachments/``.** It walks the ``attachments/<year>/<month>/``
  layout of the default storage and never looks anywhere else.
- **Young files are skipped.** An upload writes the file first and the row
  second, so a file newer than ``--min-age-hours`` (default 24) may belong to
  a request still in flight.
- **Re-checked before each delete.** The database is queried again
  immediately before a file is removed, so a row created in the meantime wins.
- **No paths in logs.** Paths are printed to the operator's terminal only,
  never sent through the application logger.

Run it against the node that holds the files: with ``FileSystemStorage`` on
per-node volumes it sees only the local node's files.

Usage::

    python manage.py purge_orphaned_attachments
    python manage.py purge_orphaned_attachments --min-age-hours 72 --yes
"""

import datetime

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from boards.models import CardAttachment

ROOT = "attachments"
_DEFAULT_MIN_AGE_HOURS = 24


def _walk(storage):
    """Yield every file name under ``attachments/<year>/<month>/``.

    ``listdir`` is not recursive, and the upload layout is exactly two levels
    deep, so walk it explicitly rather than recursing from the storage root.
    """
    try:
        years, _ = storage.listdir(ROOT)
    except (FileNotFoundError, NotADirectoryError):
        return
    for year in years:
        months, _ = storage.listdir(f"{ROOT}/{year}")
        for month in months:
            _, files = storage.listdir(f"{ROOT}/{year}/{month}")
            for name in files:
                yield f"{ROOT}/{year}/{month}/{name}"


class Command(BaseCommand):
    help = "Delete attachment files in storage that no card attachment references (dry run unless --yes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-age-hours",
            type=int,
            default=_DEFAULT_MIN_AGE_HOURS,
            help=f"Skip files modified more recently than this (default {_DEFAULT_MIN_AGE_HOURS}).",
        )
        parser.add_argument("--yes", action="store_true", help="Delete the orphans instead of only reporting them.")

    def handle(self, *args, **options):
        min_age_hours = options["min_age_hours"]
        if min_age_hours < 1:
            raise CommandError("--min-age-hours must be at least 1.")
        storage = default_storage
        cutoff = timezone.now() - datetime.timedelta(hours=min_age_hours)

        # Stream the referenced names into a set; only names are held, not rows.
        referenced = set(CardAttachment.objects.values_list("file", flat=True).iterator())

        orphans = [name for name in _walk(storage) if name not in referenced]
        found = deleted = skipped_young = 0
        for name in orphans:
            # Only orphans pay for a modified-time lookup (one HEAD on S3).
            modified = storage.get_modified_time(name)
            if timezone.is_naive(modified):
                modified = timezone.make_aware(modified)
            if modified > cutoff:
                skipped_young += 1
                continue
            found += 1
            if not options["yes"]:
                self.stdout.write(f"would delete {name}")
                continue
            # A row may have appeared since the snapshot above.
            if CardAttachment.objects.filter(file=name).exists():
                continue
            try:
                storage.delete(name)
            except Exception as exc:
                self.stderr.write(f"failed to delete {name}: {type(exc).__name__}")
                continue
            deleted += 1
            self.stdout.write(f"deleted {name}")

        verb = f"deleted {deleted}" if options["yes"] else f"{found} would be deleted (rerun with --yes)"
        self.stdout.write(f"Orphaned attachment files: {verb}; {skipped_young} too recent to touch.")
