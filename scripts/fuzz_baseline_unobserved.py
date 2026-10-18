#!/usr/bin/env python3
"""Log which schemathesis baseline entries a fuzz run did not observe (#1582).

`st run --baseline` prints only counts ("Unobserved entries: 5"), never the entry
ids, so a baseline entry that no longer reproduces cannot be pruned precisely.
The JSON run report (`--report json`) does carry them under
`baseline.unobserved_ids`; this prints those ids next to their baseline entry.

Log-only by design: it always exits 0 (including on a missing or malformed
report) so it can never change a fuzz job's pass/fail result. Output is one
header line plus one line per unobserved entry, so it cannot threaten GitLab's
16 MB log cap. Prune an entry only after the nightly deep run
(backend-schema-fuzz-deep) also lists it.

Usage:
  scripts/fuzz_baseline_unobserved.py REPORT_JSON BASELINE_JSON
  scripts/fuzz_baseline_unobserved.py --self-test
"""
import json
import sys

from _paths import cli_roots, resolve_within


def render(report, baseline):
    """Return the lines to print for a parsed report and parsed baseline."""
    info = (report or {}).get("baseline")
    if not isinstance(info, dict):
        return ["fuzz-baseline-unobserved: report has no baseline section; nothing to list."]
    ids = info.get("unobserved_ids") or []
    entries = {e.get("id"): e for e in (baseline or {}).get("entries", []) if isinstance(e, dict)}
    lines = [
        "fuzz-baseline-unobserved: %d of %d baseline entries unobserved in this run"
        " (seed %s); prune only if the nightly deep run also lists them (#1582)."
        % (len(ids), len(entries), report.get("seed"))
    ]
    for entry_id in ids:
        e = entries.get(entry_id, {})
        lines.append(
            "  unobserved %s  %s  %s/%s"
            % (entry_id, e.get("operation", "?"), e.get("check", "?"), e.get("failure", "?"))
        )
    return lines


def _run_main(argv):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(argv) == 0  # log-only: never fails the fuzz job
    return buf.getvalue()


def _self_test():
    baseline = {"entries": [
        {"id": "aaa111", "operation": "GET /x/", "check": "c1", "failure": "F1"},
        {"id": "bbb222", "operation": "POST /y/", "check": "c2", "failure": "F2"},
    ]}
    report = {"seed": 7, "baseline": {"unobserved_ids": ["bbb222", "zzz999"]}}
    out = render(report, baseline)
    assert out[0].startswith("fuzz-baseline-unobserved: 2 of 2"), out
    assert out[1] == "  unobserved bbb222  POST /y/  c2/F2", out
    assert "zzz999  ?" in out[2], out  # id missing from the file is still listed
    assert len(render({"seed": 1, "baseline": {"unobserved_ids": []}}, baseline)) == 1
    assert "no baseline section" in render({}, baseline)[0]
    assert "no baseline section" in render(None, None)[0]
    assert "skipped (PathEscapeError" in _run_main(["x", "/etc/hostname", "/etc/hostname"]), "escape not rejected"
    print("fuzz_baseline_unobserved --self-test: OK")


def main(argv):
    if argv[1:] == ["--self-test"]:
        _self_test()
        return 0
    if len(argv) != 3:
        print(__doc__)
        return 0
    try:
        # PathEscapeError is a ValueError, so a path outside the allowed roots
        # takes the same log-only skip as a missing or malformed file.
        with open(resolve_within(cli_roots(), argv[1]), encoding="utf-8") as f:
            report = json.load(f)
        with open(resolve_within(cli_roots(), argv[2]), encoding="utf-8") as f:
            baseline = json.load(f)
    except (OSError, ValueError) as exc:
        print("fuzz-baseline-unobserved: skipped (%s: %s)" % (type(exc).__name__, exc))
        return 0
    print("\n".join(render(report, baseline)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
