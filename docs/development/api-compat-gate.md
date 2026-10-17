# API compatibility gate

Visiban 1.0+ treats the REST schema and the operator-facing environment variable names as a
public contract (see "Backward compatibility" in `CLAUDE.md`). The `api-compat-check` CI job
(#1667) diffs both against the last GA tag on every MR and on `main`, so a removal can no
longer slip through on a green pipeline.

It is a sibling of `backend-schema-validate` (is the document well formed?) and
`backend-schema-fuzz` (does the API match the document?). This job asks a third question: does
today's contract still contain everything the last release promised?

## What it checks

**OpenAPI** — the schema `manage.py spectacular` emits for the branch is compared with
`scripts/compat-baseline/v1.1.0/openapi.json`. `$ref` and `allOf` are resolved on both sides,
so a renamed component is not a finding; only what a client can observe is.

| Finding | Meaning |
|---|---|
| `REMOVED_OPERATION` | A path + method present in the baseline is gone |
| `REMOVED_RESPONSE_STATUS` | A 2xx status the baseline documented is gone |
| `REMOVED_RESPONSE_FIELD` / `REMOVED_REQUEST_FIELD` | A property was removed |
| `TYPE_CHANGED` | A property, parameter, or body changed type |
| `RESPONSE_FIELD_BECAME_OPTIONAL` | A response property is no longer always present |
| `RESPONSE_BECAME_NULLABLE` | A response property may now be `null` |
| `REQUEST_FIELD_BECAME_REQUIRED`, `NEW_REQUIRED_REQUEST_FIELD`, `NEW_REQUIRED_REQUEST_BODY` | Existing clients now fail validation |
| `REQUEST_STOPPED_ACCEPTING_NULL`, `REQUEST_ENUM_VALUE_REMOVED` | A request value that used to be accepted is not |
| `REMOVED_PARAMETER`, `PARAMETER_BECAME_REQUIRED`, `NEW_REQUIRED_PARAMETER`, `PARAMETER_TYPE_CHANGED` | The same, for query/header/path parameters |
| `REMOVED_REQUEST_MEDIA_TYPE`, `REMOVED_RESPONSE_MEDIA_TYPE` | A content type is no longer offered |

Additions (new endpoints, new optional fields, new optional parameters) always pass. Error
statuses (4xx/5xx) are not part of the checked contract. A baseline property that is a bare
read-only `string` is treated as untyped: it is drf-spectacular's fallback for an unannotated
`SerializerMethodField`, so correcting it to its real type is not a break.

**Settings** — every env var name that `backend/visiban/settings.py` reads at the GA tag
(`env(...)`, `env.<type>(...)`, `_secret_env(...)`, `os.environ`/`os.getenv`, and the legacy
name passed to `_warn_deprecated_env_alias`) must still be read at HEAD. A deprecated alias
that is kept counts as present, which is exactly the rename policy in `CLAUDE.md`. The
baseline is `scripts/compat-baseline/v1.1.0/settings-env.txt`.

## Allow-listing a deliberate break

A break that is intentional (a planned major bump, or a removal of an item already deprecated
for a full release) goes in `scripts/compat-baseline/allowlist.json`. Every entry needs an
`issue` (`#1234`) and a `reason`, and exactly one target:

```json
{"allow": [
  {"key": "REMOVED_RESPONSE_FIELD GET /api/v1/boards/ response 200 application/json .results[] .legacy_name",
   "issue": "#1700", "reason": "deprecated in 1.2, removal scheduled for 2.0"},
  {"operation": "POST /api/v1/old/", "issue": "#1701", "reason": "endpoint retired after the 410 period"}
]}
```

- `key` is the exact text the gate prints (`*` is a wildcard).
- `operation` (`"METHOD /path/"`) covers every finding under that one operation.

An entry that no longer matches any finding is **stale and fails the job**, so the list can
only shrink toward the truth. Entries without an issue reference fail the job outright.

Prefer `key` entries. An `operation` entry silently covers every future finding under that
operation, so use it only when the whole operation is deliberately retired (the five
`#1080` catch-all entries are an example). The self-test pins the opposite behavior for
`key` entries: a *new* finding under an operation that already has entries still fails.

### Frozen snapshot of the v1.1.0 baseline drift

The v1.1.0 schema was generated before the schema-hygiene work (#1135-#1138, #1209, #1080) and
documented fallback or wrong serializers for a number of operations (for example a card
serializer standing in for the request body of `checklist/` and `comments/`). Diffing against
it yields findings that look like documentation corrections rather than runtime changes.

A read-only comparison of the v1.1.0 and current runtime code verified these classes as
**documentation-only baseline errors, not breaks**:

- `card_count` / `member_count` on `GET /boards/{id}/full/`: the v1.1.0 `BoardFullSerializer`
  never had them; the v1.1.0 schema showed the viewset's default `BoardSerializer`.
- `assignee` / `created_by` nullable on cards: v1.1.0 already returned `null` for the
  `SET_NULL` foreign keys; only the schema (`allow_null=True`) changed.
- `POST /boards/import/` `file`: v1.1.0 was already multipart-only and answered 400
  "No file provided." without it.
- `object` -> `array` list responses (activities, attachments, movements, saved-filters, and
  the group boards / descendant-boards / invite-links / labels / subgroups actions): these
  hand-written `@action`s returned bare arrays in both versions; only the schema's
  `pagination_class` was fixed.
- 2xx status changes: an AST scan found none that differ at runtime.

The same comparison found no serializer field and no URL route removed between v1.1.0 and
`main` (the `dj_rest_auth` `include()` became explicit routes, and five declarations were
rewritten as same-name `SerializerMethodField`s). **All other entries remain unverified.**

Those findings are therefore frozen as exact per-finding `key` entries under `#1695`, the burn-down tracker,
(generated once from the gate's own output with an empty allow-list, sorted for stable
diffs). The snapshot is closed: anything not already in it, including a further change to an
operation that has entries, fails the job. Never add to it to get a pipeline green; a new
finding is a real break or needs its own issue-referenced entry.

**Burn-down workflow** (tracked in #1695). Pick an operation, check its v1.1.0 behavior against the current
runtime (serializer, status code, response shape). If it is only a documentation correction,
delete that operation's entries and confirm the job stays green (a deleted entry that still
matches a finding turns the job red, which is the signal that the break is real and needs a
fix or a properly justified entry). Entries should only ever be deleted, never added. The next
GA baseline is generated from a corrected schema and does not carry this debt.

## Running it locally

```bash
cd backend && source .venv/bin/activate   # the same env the backend tests use
cd .. && python3 scripts/check-api-compat.py --self-test
python3 scripts/check-api-compat.py
bash scripts/tests/check-api-compat.test.sh
```

## Regenerating the baseline (at each GA tag)

Do this once, when a new GA tag is cut, from a clean checkout of the tag. Run it in a
temporary worktree outside the repo and delete it afterward.

```bash
TAG=v1.2.0
VENV_PY="$PWD/backend/.venv/bin/python"   # run from the repository root
git worktree add --detach /tmp/visiban-$TAG $TAG
( cd /tmp/visiban-$TAG/backend \
  && DATABASE_URL=postgres://u:p@localhost:5432/d REDIS_URL=redis://localhost:6379/0 \
     DJANGO_SECRET_KEY=x DEBUG=True ALLOWED_HOSTS=localhost GIT_LENS_ENABLED=True \
     "$VENV_PY" manage.py spectacular --format openapi-json --file /tmp/$TAG.json )
mkdir -p scripts/compat-baseline/$TAG
python3 scripts/check-api-compat.py --normalize /tmp/$TAG.json scripts/compat-baseline/$TAG/openapi.json
python3 scripts/check-api-compat.py --dump-env /tmp/visiban-$TAG/backend/visiban/settings.py \
  > scripts/compat-baseline/$TAG/settings-env.txt
git worktree remove /tmp/visiban-$TAG
```

Then, as a manual step on the
release checklist, point `BASELINE_SCHEMA` / `BASELINE_ENV` in `scripts/check-api-compat.py`
(lines 52-53) at the new
directory, clear the allow-list entries the new baseline no longer needs, and delete the old
baseline directory. `--normalize` strips prose, examples, and `x-` extensions so the fixture
stays small and docs edits never read as contract changes.

## Not covered

- Serializer fields that never reach the schema (the schema is the source of truth here;
  `serializer-ts-parity` covers frontend interfaces).
- WebSocket events: see [WebSocket event contract](websocket-event-contract.md).
- Settings *values* and defaults; only names are compared.
