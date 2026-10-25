# #1880: the hash public share links are resolved by. Nullable with no index:
# 0088 backfills it from existing tokens, and the unique constraint is built
# concurrently in 0089, not under this migration's table lock.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0086_normalize_board_export_min_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="board",
            name="share_token_hash",
            field=models.CharField(blank=True, default=None, editable=False, max_length=64, null=True),
        ),
    ]
