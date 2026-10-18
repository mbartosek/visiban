# #1704: the account an emailed board invite is waiting on until it confirms
# the invited address under EMAIL_VERIFICATION=mandatory.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0075_invite_recipient_hash"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Nullable, no default needed: no existing invite is waiting on anyone.
        # db_index=False — the partial index is built concurrently in 0077, not
        # under this migration's table lock.
        migrations.AddField(
            model_name="boardinvitelink",
            name="reserved_for",
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
