# #1865: the admin who enabled a board's public share link.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0082_cardcomment_imported_from_username"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Nullable, no default needed: links enabled before this field existed
        # record no enabler. db_index=False — the partial index is built
        # concurrently in 0084, not under this migration's table lock.
        migrations.AddField(
            model_name="board",
            name="share_enabled_by",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
