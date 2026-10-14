"""In-process provider stub for the git_lens schema-fuzz job (#1338).

``GET /api/v1/git-lens/board/{id}/`` fetches issues from GitHub/GitLab. Fuzzing it
from CI against the real APIs would mean outbound network calls on every case and
burning gitlab.com's anonymous per-IP rate limit for the runner's address. With
``VISIBAN_GIT_LENS_STUB_PROVIDERS`` on, ``providers._provider_get`` asks this
module for the response instead of calling ``requests.get``.

Why the stub sits at the HTTP leg, not at the provider registry: everything after
the network call still runs for real, namely the provider's status-code mapping
(``raise_for``), normalization, pipeline linking, pivoting, the outbound metrics
and the view's cache/budget logic. So the fuzzed response is what a real fetch of
this fixture would produce, not a hand-built payload that could drift from it.

Production safety, three layers, each failing closed:

1. ``settings.py`` refuses to start when the variable is set without DEBUG=True
   or with DEMO_MODE on (same contract as VISIBAN_DISABLE_THROTTLING, #1569).
2. ``enabled()`` re-checks both the setting and DEBUG at the call site, so a
   settings override that bypassed (1) still sends real requests rather than
   fixture data. Anything other than a literal ``True`` reads as off.
3. The stub only ever *returns* canned public-issue data. It holds no
   credentials, ignores the request's token and URL, and makes no network call,
   so even a misconfigured install could at worst show fixture issues on a lens
   board. It cannot widen anyone's access.
"""
from __future__ import annotations

from django.conf import settings

# GitLab-shaped issues (``with_labels_details=true``), chosen to put something in
# every pipeline column and to carry the nullable fields (milestone due date,
# empty assignees, a plain-string label) the response has to tolerate.
_GITLAB_ISSUES = [
    {
        "iid": 1,
        "title": "Fixture: open issue with an active milestone",
        "web_url": "https://gitlab.com/visiban/fuzz-fixture/-/issues/1",
        "state": "opened",
        "labels": [{"name": "status::todo", "color": "#1f75cb"}, {"name": "bug", "color": None}],
        "assignees": [{"username": "fixture-user", "avatar_url": ""}],
        "milestone": {"title": "1.0", "due_date": "2026-12-31", "state": "active"},
    },
    {
        "iid": 2,
        "title": "Fixture: issue with a linked branch",
        "web_url": "https://gitlab.com/visiban/fuzz-fixture/-/issues/2",
        "state": "opened",
        "labels": ["plain-label"],
        "assignees": [],
        "milestone": {"title": "1.0", "due_date": None, "state": "active"},
    },
    {
        "iid": 3,
        "title": "Fixture: issue with an open merge request",
        "web_url": "https://gitlab.com/visiban/fuzz-fixture/-/issues/3",
        "state": "opened",
        "labels": [],
        "assignees": [],
        "milestone": None,
    },
    {
        "iid": 4,
        "title": "Fixture: closed issue",
        "web_url": "https://gitlab.com/visiban/fuzz-fixture/-/issues/4",
        "state": "closed",
        "labels": [],
        "assignees": [],
        "milestone": None,
    },
]

_GITLAB_FIXTURES = {
    "issues": _GITLAB_ISSUES,
    "branches": [{"name": "2-fixture-branch"}],
    "merge_requests": [
        {
            "iid": 10,
            "web_url": "https://gitlab.com/visiban/fuzz-fixture/-/merge_requests/10",
            "source_branch": "3-fixture-mr",
            "title": "Fixture MR",
            "description": "Closes #3",
        }
    ],
}


class StubResponse:
    """The subset of ``requests.Response`` the providers read."""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


def enabled() -> bool:
    """True only when the CI switch is on AND DEBUG is on (see module docstring)."""
    return (
        getattr(settings, "VISIBAN_GIT_LENS_STUB_PROVIDERS", False) is True
        and settings.DEBUG is True
    )


def response(provider: str, kind: str, params: dict | None) -> StubResponse:
    """Canned response for one outbound leg.

    GitLab gets the fixture above on page 1 and an empty page after it. Every other
    provider/kind gets an empty list: the GitHub path needs the viewer's own OAuth
    token, which the fuzz user does not have, so it answers 409 before any fetch.
    An empty list is still a valid, well-formed answer if that ever changes.
    """
    page = (params or {}).get("page", 1)
    if provider == "gitlab" and page == 1:
        return StubResponse(_GITLAB_FIXTURES.get(kind, []))
    return StubResponse([])
