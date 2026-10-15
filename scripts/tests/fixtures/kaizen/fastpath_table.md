# Fixture: a copy of the fast-path table shape from the global CLAUDE.md (#1145)

| Change class | Required gates (in order) |
|---|---|
| Pure bugfix with identified root cause | `regression-check` → `test-scaffold` (if no coverage) → `completeness-check` → `changelog` → `/mr` |
| Dependency bump (single package, no API change) | `dependency` → `changelog` → `/mr` |
| Docs-only | `docs-writer` (if new page -> see docs/)  ->`completeness-check`  →  `/mr` |
| Chore / CI config / lint-only | `/mr` |
| Backend-only feature (new endpoint or model) | `architect` → batched pre-MR gate cluster (see below) → `test-scaffold` → `completeness-check` → `changelog` → `/mr` |
| Frontend-only feature (no API change) | `architect` → `ux-design` → implement → `ux-review` → `test-scaffold` → `completeness-check` → `changelog` → `/mr` |
| Settings sub-page wiring (form/toggle on an existing serializer) | `ux-review` (visual diff) → `rbac-check` + `security-review` + `regression-check` → `test-scaffold` → `completeness-check` → `changelog` → `/mr` |
| New user-visible feature (full stack) | `voice-of-customer` → `threat-model` (only if the feature crosses a trust boundary — see Gate reference) → `architect` → `ux-design` → implement → batched pre-MR gate cluster → `ux-review` → `test-scaffold` → `completeness-check` → `changelog` → `/mr` |

When a change spans rows, take the union.
