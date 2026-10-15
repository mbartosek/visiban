# Fixture: cells the proposer must refuse to edit (#1145)

| Change class | Required gates (in order) |
|---|---|
| Pure bugfix with identified root cause | `architect` → `regression-check` (if x) or `perf-check` (if y) → `/mr` |
| Docs-only | `completeness-check` + `completeness-check` → `/mr` |
| Backend-only feature (new endpoint or model) | `regression-check` → `completeness-check` → `/mr` |
