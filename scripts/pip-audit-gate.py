#!/usr/bin/env python3
"""scripts/pip-audit-gate.py -- accepted-risk gate for the backend-dep-scan CI job.

Bare `pip-audit` has no awareness of backend/osv-scanner.toml, so an advisory
with no fix (twisted GHSA-8pqf-f4m5-798g, #1688) red-walled every nightly run
even though dep-scan-osv already accepts it (#1685). This gate reads
`pip-audit --format json` and fails on any vulnerability not accepted in
backend/osv-scanner.toml -- the single source of truth for accepted risks, so
the two scanners can never disagree. It is the backend counterpart of
scripts/npm-audit-gate.mjs (#1415).

pip-audit may report an advisory under a CVE id with the GHSA only in
`aliases` (or vice versa), so each vulnerability is matched on its `id` AND
every alias. An entry past its `ignoreUntil` date stops suppressing, forcing
the same reassessment OSV does; an entry with no date is accepted indefinitely.
pip-audit emits no severity, so every unaccepted vulnerability blocks.

Exit codes: 0 clean, 1 unaccepted vulnerability (or unparseable input), 3 usage.
Usage: python3 scripts/pip-audit-gate.py <pip-audit.json> [osv-scanner.toml] [today=YYYY-MM-DD]
       python3 scripts/pip-audit-gate.py --self-test
  --self-test proves the gate still fires on a known-bad report (#1093): a
  gate that silently stops detecting looks identical to a clean codebase.
"""
import datetime
import json
import sys
import tomllib

from _paths import cli_roots, resolve_within


def accepted_ids(toml_text, today):
    """Return the set of advisory ids still inside their ignoreUntil window."""
    ids = set()
    for entry in tomllib.loads(toml_text).get("IgnoredVulns", []):
        vuln_id = entry.get("id")
        until = entry.get("ignoreUntil")
        # No date means accepted indefinitely. TOML dates parse to datetime.date;
        # normalize to ISO strings so they compare lexically with `today`.
        if until is not None:
            until = until.isoformat() if hasattr(until, "isoformat") else str(until)[:10]
        if vuln_id and (until is None or until >= today):
            ids.add(vuln_id)
    return ids


def blocking_vulns(report, accepted):
    """Return report lines for vulns whose id and aliases are all unaccepted.

    Raises ValueError when the report has no `dependencies` list: an
    incomplete scan must block, not pass.
    """
    deps = report.get("dependencies") if isinstance(report, dict) else None
    if not isinstance(deps, list):
        raise ValueError("report has no 'dependencies' list")
    found = []
    for dep in deps:
        # Skipped dependencies carry `skip_reason` and no `vulns`.
        for vuln in dep.get("vulns") or []:
            names = {vuln.get("id"), *(vuln.get("aliases") or [])}
            if names & accepted:
                continue
            fixes = ", ".join(vuln.get("fix_versions") or []) or "no fix versions"
            found.append(f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')} ({fixes})")
    return found


def self_test():
    def report(vuln_id, aliases):
        return {"dependencies": [
            {"name": "skipped", "version": "1", "skip_reason": "not on PyPI"},
            {"name": "pkg", "version": "1.0", "vulns": [
                {"id": vuln_id, "fix_versions": [], "aliases": aliases}]},
        ]}

    toml_text = '[[IgnoredVulns]]\nid = "GHSA-accepted"\nignoreUntil = 2026-11-30\n'
    live = accepted_ids(toml_text, "2026-10-03")
    expired = accepted_ids(toml_text, "2026-12-01")
    undated = accepted_ids('[[IgnoredVulns]]\nid = "GHSA-forever"\n', "2099-01-01")

    def unparseable():
        try:
            blocking_vulns({}, set())
        except ValueError:
            return True
        return False

    cases = [
        ("unaccepted vuln is flagged", len(blocking_vulns(report("CVE-bad", []), live)) == 1),
        ("accepted id is spared", len(blocking_vulns(report("GHSA-accepted", []), live)) == 0),
        ("accepted alias is spared (twisted CVE/GHSA case)",
         len(blocking_vulns(report("CVE-2026-0001", ["GHSA-accepted"]), live)) == 0),
        ("expired ignore is flagged", len(blocking_vulns(report("GHSA-accepted", []), expired)) == 1),
        ("undated ignore is indefinite", len(blocking_vulns(report("GHSA-forever", []), undated)) == 0),
        ("report without dependencies fails safe", unparseable()),
        ("report path outside the allowed roots blocks", main(["/etc/hostname"]) == 1),
    ]
    print("=== pip-audit-gate.py --self-test ===")
    failed = [name for name, ok in cases if not ok]
    for name, ok in cases:
        print(f"{'OK' if ok else 'FAIL'}: {name}")
    if failed:
        print(f"=== pip-audit-gate.py --self-test: FAILED ({len(failed)}) ===", file=sys.stderr)
        return 1
    print("=== pip-audit-gate.py --self-test: PASSED ===")
    return 0


def main(argv):
    if argv and argv[0] == "--self-test":
        return self_test()
    if not argv:
        print("usage: pip-audit-gate.py <pip-audit.json> [osv-scanner.toml] [today]", file=sys.stderr)
        return 3
    audit_path = argv[0]
    toml_path = argv[1] if len(argv) > 1 else "backend/osv-scanner.toml"
    today = argv[2] if len(argv) > 2 else datetime.date.today().isoformat()
    try:
        with open(resolve_within(cli_roots(), audit_path), encoding="utf-8") as fh:
            report = json.load(fh)
        # Validate the shape before touching the TOML so a bad report always blocks.
        blocking_vulns(report, set())
    except (OSError, ValueError) as exc:
        print(f"pip-audit-gate: cannot parse {audit_path}: {exc}", file=sys.stderr)
        return 1  # fail safe: an incomplete scan must block, not pass
    try:
        with open(resolve_within(cli_roots(), toml_path), encoding="utf-8") as fh:
            accepted = accepted_ids(fh.read(), today)
    except FileNotFoundError:
        accepted = set()  # no accepted risks
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        print(f"pip-audit-gate: cannot parse {toml_path}: {exc}", file=sys.stderr)
        return 1
    blocking = blocking_vulns(report, accepted)
    if blocking:
        print("pip-audit-gate: unaccepted vulnerabilities:", file=sys.stderr)
        for line in blocking:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"pip-audit-gate: clean ({len(accepted)} accepted advisory id(s) honored)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
