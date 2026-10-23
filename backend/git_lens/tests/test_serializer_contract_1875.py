"""Pin the frozen LensConnection payload shape (#1875).

The Git Lens REST operations are excluded from the OpenAPI compat gate
(scripts/compat-baseline/experimental.json), but the LensConnection object
rides the frozen `lens_connection.configured` WebSocket event, so its field set
is public contract. With its REST operations excluded, nothing else mechanical
guards it; this test does. Adding a field is fine (update the tuple); removing
or renaming one needs a major bump.
"""
import pytest
from django.conf import settings

if not settings.GIT_LENS_ENABLED:  # pragma: no cover - exercised only in the flagged CI job
    pytest.skip(
        "git_lens experiment disabled; set GIT_LENS_ENABLED=true to run these.",
        allow_module_level=True,
    )

from git_lens.serializers import LensConnectionSerializer

FROZEN_FIELDS = (
    "id",
    "provider",
    "repo_slug",
    "column_dim",
    "swimlane_dim",
    "created_by",
    "created_at",
    "updated_at",
)


def test_lens_connection_serializer_fields_are_frozen():
    assert tuple(LensConnectionSerializer.Meta.fields) == FROZEN_FIELDS
