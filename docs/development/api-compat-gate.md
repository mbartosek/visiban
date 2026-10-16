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

### Known baseline inaccuracy (v1.1.0)

The v1.1.0 schema was generated before the schema-hygiene work (#1135-#1138, #1209, #1080) and
documented fallback or wrong serializers for a number of operations (for example a card
serializer standing in for the request body of `checklist/` and `comments/`). Diffing
against it produces findings that are documentation corrections, not runtime changes. Those
operations are allow-listed under `#1667` with a note that per-operation runtime
verification is pending; as each is verified (or fixed if it was a real break) its entry
should be deleted. The next GA baseline is generated from a corrected schema and does not
carry this debt.

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
git worktree add --detach /tmp/visiban-$TAG $TAG
( cd /tmp/visiban-$TAG/backend \
  && DATABASE_URL=postgres://u:p@localhost:5432/d REDIS_URL=redis://localhost:6379/0 \
     DJANGO_SECRET_KEY=x DEBUG=True ALLOWED_HOSTS=localhost GIT_LENS_ENABLED=True \
     /path/to/backend/.venv/bin/python manage.py spectacular --format openapi-json --file /tmp/$TAG.json )
mkdir -p scripts/compat-baseline/$TAG
python3 scripts/check-api-compat.py --normalize /tmp/$TAG.json scripts/compat-baseline/$TAG/openapi.json
python3 scripts/check-api-compat.py --dump-env /tmp/visiban-$TAG/backend/visiban/settings.py \
  > scripts/compat-baseline/$TAG/settings-env.txt
git worktree remove /tmp/visiban-$TAG
```

Then point `BASELINE_SCHEMA` / `BASELINE_ENV` in `scripts/check-api-compat.py` at the new
directory, clear the allow-list entries the new baseline no longer needs, and delete the old
baseline directory. `--normalize` strips prose, examples, and `x-` extensions so the fixture
stays small and docs edits never read as contract changes.

## Not covered

- Serializer fields that never reach the schema (the schema is the source of truth here;
  `serializer-ts-parity` covers frontend interfaces).
- WebSocket events: see [WebSocket event contract](websocket-event-contract.md).
- Settings *values* and defaults; only names are compared.
