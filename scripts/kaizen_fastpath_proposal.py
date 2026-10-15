#!/usr/bin/env python3
"""Propose fast-path table edits from measured gate yield (issue #1145).

The fast-path change-class table (which change class runs which gates) was
hand-maintained: the "Settings sub-page wiring" row exists because someone
noticed kaizen finding #682 and wrote it by hand. #1144 made gate yield a
continuously updated signal (`.claude/kaizen-yield-state.json`, a per
(gate, diff-shape) streak of consecutive `0 findings` runs). This script reads
that state and proposes the table edits the data supports.

It NEVER edits the table. It prints a unified diff on stdout (apply it with
`patch`, in a normal reviewed MR) and a ready-to-paste `## Fast-path evidence`
MR-description section (to --evidence-out, else stderr) citing gate, diff
shape, streak, and the MRs in the streak, the way #682 is cited today.

What it can propose, and what it cannot:
  * narrow a row: a gate named in a row's "Required gates" cell whose streak
    on that row's diff shape reached the threshold is removed from the cell.
  * a gate covered only by the row's "batched pre-MR gate cluster" wording,
    or a shape with no mapped row (`other`), is reported as evidence with no
    diff -- the edit needs a human (the cluster's applicability list is prose).
  * removing a gate from every row that runs it is a gate deletion, which
    /kaizen's hard rule reserves for a human: reported, never diffed.
  * a cell that cannot be split safely (unbalanced parentheses or backticks,
    the gate named inside prose) is reported, never diffed.
  * adding a row is out of reach of this data: diff shape is the branch
    prefix (feat/fix/docs/chore), which cannot tell a sub-class of `feat`
    (like Settings sub-page wiring) apart from the rest. That scope cut is
    tracked in #1652 (path-based diff shapes); until then /kaizen stays the
    fallback for that kind of finding.

Reuses kaizen_yield_watch's state, threshold, and declined ledger; a gate
declined as `<gate>:fast-path-candidate` in .claude/kaizen-declined.json is
not proposed. Unlike the watch's issue filing, a pair that already had an
issue filed is still proposed: the issue and the table edit are separate
steps.

The table lives in the user's global ~/.claude/CLAUDE.md, outside the repo, so
--table is the normal invocation; without it the repo's CLAUDE.md files are
searched and the script exits 2 if none holds the table.

Usage:
  python3 scripts/kaizen_fastpath_proposal.py --table ~/.claude/CLAUDE.md > fastpath.diff
  python3 scripts/kaizen_fastpath_proposal.py --table T --evidence-out evidence.md
  python3 scripts/kaizen_fastpath_proposal.py --self-test
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kaizen_yield_watch as watch  # noqa: E402
from _paths import PathEscapeError, cli_roots, resolve_within  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_CLAUDE = Path.home() / ".claude"
HEADER_RE = re.compile(r"^\|\s*Change class\s*\|\s*Required gates")
# Top-level separators in a gates cell: a step arrow (`→` or `->`) or a `+`
# joining gates in one step, with whatever whitespace surrounds them.
SEP_RE = re.compile(r"\s*(?:→|->|\+)\s*")
CLUSTER = "pre-MR gate cluster"

# Diff shape (branch prefix, see kaizen_yield_watch.diff_shape) -> the table
# rows it covers, matched by case-insensitive prefix of the "Change class"
# cell. `feat` spans every feature row: the branch prefix cannot tell them
# apart, so a feat streak is evidence about all of them together.
SHAPE_ROWS = {
    "fix": ("pure bugfix",),
    "docs": ("docs-only",),
    "chore": ("chore", "dependency bump"),
    "feat": (
        "backend-only feature",
        "frontend-only feature",
        "settings sub-page",
        "new user-visible feature",
    ),
}
# Ledger name -> table name, where the two spellings differ.
TABLE_NAME = {"voc": "voice-of-customer"}
# Gates the table names only through "batched pre-MR gate cluster".
PRE_MR_CLUSTER = {
    "regression-check",
    "security-review",
    "rbac-check",
    "perf-check",
    "broadcast-check",
    "migration-check",
}


class TableNotFound(Exception):
    pass


def find_table(lines):
    """(start, end) line indexes of the table body rows, or None."""
    for i, line in enumerate(lines):
        if HEADER_RE.match(line):
            start = i + 2  # skip the |---|---| separator
            end = start
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
            return start, end
    return None


def split_row(line):
    cells = line.strip().strip("|").split("|")
    return cells[0].strip(), "|".join(cells[1:]).strip()


def default_table():
    """First repo CLAUDE.md (root, then depth 1-2, sorted) that holds the table."""
    paths = [
        REPO_ROOT / "CLAUDE.md",
        *sorted(REPO_ROOT.glob("*/CLAUDE.md")),
        *sorted(REPO_ROOT.glob("*/*/CLAUDE.md")),
    ]
    for p in paths:
        if p.is_file() and find_table(p.read_text(encoding="utf-8").splitlines()):
            return p
    raise TableNotFound(
        "no fast-path table (`| Change class | Required gates ...`) in the repo's CLAUDE.md files; "
        "pass --table PATH (it normally lives in ~/.claude/CLAUDE.md)"
    )


class Unsafe(ValueError):
    """The cell cannot be split safely; refuse the edit rather than guess."""


def tokenize(cell):
    """Split a gates cell into [item, sep, item, sep, ..., item].

    Separators are `→` or `->` (a step) and `+` (gates run together), each with
    any surrounding whitespace, and only at the top level: an arrow inside a
    parenthesized qualifier or a backtick span is part of the item. Separators
    keep their original spacing so an edit changes nothing but the removed item.
    Raises Unsafe on unbalanced parentheses or backticks.
    """
    tokens, buf, depth, tick, i = [], "", 0, False, 0
    while i < len(cell):
        c = cell[i]
        if c == "`":
            tick = not tick
        elif not tick and c == "(":
            depth += 1
        elif not tick and c == ")":
            depth -= 1
            if depth < 0:
                raise Unsafe("unbalanced parentheses")
        elif not tick and depth == 0:
            m = SEP_RE.match(cell, i)
            if m:
                tokens += [buf, m.group(0)]
                buf, i = "", m.end()
                continue
        buf += c
        i += 1
    if depth or tick:
        raise Unsafe("unbalanced parentheses or backticks")
    tokens.append(buf)
    if any(not t.strip() for t in tokens[::2]):
        raise Unsafe("empty step")
    return tokens


def names_item(item, gate):
    """True when *item* is `gate`, optionally followed by a (qualifier)."""
    m = re.fullmatch(r"\s*`([^`]+)`\s*(\(.*\))?\s*", item, re.S)
    return bool(m) and m.group(1) == gate


def remove_gate(cell, gate):
    """Return *cell* without the item naming *gate*, or None if not named.

    The whole item goes, qualifier included ("`test-scaffold` (if no
    coverage)"), with one adjacent separator: a `+` one if the item sits in a
    `+` group (so the rest of the group stays one step), else the preceding
    arrow (the following one for a first item). Raises Unsafe when the gate is
    named but not as a standalone item, or the cell cannot be split safely, so
    the caller reports it instead of writing broken markdown.
    """
    tokens = tokenize(cell)
    hits = [k for k in range(0, len(tokens), 2) if names_item(tokens[k], gate)]
    if not hits:
        if f"`{gate}`" in cell:
            raise Unsafe("named, but not as a standalone item")
        return None
    if len(tokens) == 1:
        raise Unsafe("it is the only item in the row")
    for k in reversed(hits):
        seps = [j for j in (k - 1, k + 1) if 0 < j < len(tokens)]
        plus = [j for j in seps if tokens[j].strip() == "+"]
        drop = plus[0] if plus else seps[0]
        lo, hi = min(k, drop), max(k, drop)
        del tokens[lo : hi + 1]
    return "".join(tokens)


def appears(cell, name):
    """Does a row run *name*, by name or through the pre-MR gate cluster?"""
    return f"`{name}`" in cell or (name in PRE_MR_CLUSTER and CLUSTER in cell)


def build(state, declined, text):
    """Return (new_text, edits, notes); pure, so tests need no files.

    One entry per candidate pair. edits: (gate, shape, pair, rows narrowed);
    notes: (gate, shape, pair, why no automatic edit). The threshold is the
    state file's own, so a state written with another threshold is honored.
    """
    lines = text.splitlines(keepends=True)
    span = find_table([ln.rstrip("\n") for ln in lines])
    if span is None:
        raise TableNotFound("table file has no fast-path table")
    rows = list(range(*span))
    edits, notes = [], []
    threshold = state.get("threshold", watch.THRESHOLD)
    for key, p in sorted(state["pairs"].items()):
        gate, shape = key.split("|")
        if p["streak"] < threshold or f"{gate}:fast-path-candidate" in declined:
            continue
        name = TABLE_NAME.get(gate, gate)
        prefixes = SHAPE_ROWS.get(shape)
        if not prefixes:
            notes.append((gate, shape, p, f"diff shape `{shape}` maps to no table row"))
            continue
        planned, cluster, unsafe = {}, [], []
        for i in rows:
            label, cell = split_row(lines[i])
            if not label.lower().startswith(prefixes):
                continue
            short = label.split(" (")[0]
            try:
                narrowed = remove_gate(cell, name)
            except Unsafe as exc:
                unsafe.append(f"{short} ({exc})")
                continue
            if narrowed is not None:
                planned[i] = (label, short, narrowed)
            elif name in PRE_MR_CLUSTER and CLUSTER in cell:
                cluster.append(short)
        everywhere = [i for i in rows if appears(split_row(lines[i])[1], name)]
        deletion = bool(planned) and set(everywhere) <= set(planned)
        if deletion:
            # Removing it from every row that runs it deletes the gate from
            # the table; /kaizen's hard rule (narrow, never delete) reserves
            # that call for a human.
            named = "; ".join(s for _, s, _ in planned.values())
            notes.append(
                (
                    gate,
                    shape,
                    p,
                    f"would remove it from every row that runs it ({named}): a gate "
                    "deletion, which /kaizen's hard rule reserves for a human (no diff)",
                )
            )
            planned = {}
        for i, (label, _, narrowed) in planned.items():
            lines[i] = f"| {label} | {narrowed} |\n"
        if planned:
            edits.append((gate, shape, p, "; ".join(s for _, s, _ in planned.values())))
        if unsafe:
            notes.append(
                (
                    gate,
                    shape,
                    p,
                    f"row not edited, cell could not be split safely: {'; '.join(unsafe)}",
                )
            )
        if cluster:
            notes.append(
                (
                    gate,
                    shape,
                    p,
                    f"only via the pre-MR gate cluster in: {'; '.join(cluster)} (hand edit)",
                )
            )
        if not (planned or deletion or unsafe or cluster):
            notes.append(
                (
                    gate,
                    shape,
                    p,
                    f"no `{shape}` row names it; it ran outside the table (nothing to narrow)",
                )
            )
    return "".join(lines), edits, notes


def evidence_md(state, edits, notes, source):
    threshold = state.get("threshold", watch.THRESHOLD)
    mark = state.get("last_iid") or 0
    out = [
        "## Fast-path evidence",
        "",
        f"Generated by `scripts/kaizen_fastpath_proposal.py` (#1145) from `{source}`, "
        f"watermark !{mark} ({state.get('last_merged_at') or 'none'}). Threshold: {threshold} consecutive "
        "`0 findings` runs of a gate on one diff shape (the `/kaizen` MIN_RUNS_FOR_VERDICT). "
        "`n/a` and `skipped` ledger lines are not runs. Nothing was applied; this MR is the review.",
        "",
    ]

    def row(gate, shape, p, where):
        mrs = ", ".join(f"!{i}" for i in p["evidence"]) or "none recorded"
        return f"| `{gate}` | `{shape}` | {p['streak']} | {p['runs']} ({p['positive']} with findings) | {mrs} | {where} |"

    head = "| Gate | Diff shape | Consecutive 0-finding runs | Lifetime runs | Streak MRs (latest) | {} |"
    if edits:
        out += [head.format("Row narrowed"), "|---|---|---|---|---|---|"]
        out += [row(g, s, p, label) for g, s, p, label in edits]
        out += ["", "Before merging, for each narrowed gate:", ""]
        out += [
            f"- [ ] `{g}`: also review the Gate reference row and any prose restating when this gate fires"
            for g in dict.fromkeys(g for g, _, _, _ in edits)
        ]
        out.append("")
    else:
        out += ["No table edit proposed.", ""]
    if notes:
        out += [
            "Candidates with no automatic edit:",
            "",
            head.format("Why"),
            "|---|---|---|---|---|---|",
        ]
        out += [row(g, s, p, why) for g, s, p, why in notes]
        out.append("")
    out += [
        "A streak is absence of evidence: confirm the gate was not only run on trivially safe diffs. "
        "Narrow a trigger, never delete a gate. `/kaizen` remains the manual cross-check "
        "(see docs/development/kaizen-fastpath-proposal.md).",
        "",
    ]
    return "\n".join(out)


def run(args, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    roots = cli_roots()
    state = watch.load_state(args.state)
    declined = watch.load_declined(args.declined)
    table = (
        resolve_within(cli_roots(HOME_CLAUDE), args.table)
        if args.table
        else default_table()
    )
    text = table.read_text(encoding="utf-8")
    new, edits, notes = build(state, declined, text)
    label = args.table or os.path.relpath(table, REPO_ROOT)
    out.writelines(
        difflib.unified_diff(text.splitlines(True), new.splitlines(True), label, label)
    )
    src = Path(args.state).resolve()
    md = evidence_md(
        state,
        edits,
        notes,
        src.relative_to(REPO_ROOT) if src.is_relative_to(REPO_ROOT) else args.state,
    )
    print(
        f"kaizen-fastpath-proposal: {len({(g, s) for g, s, _, _ in edits + notes})} candidate(s) over "
        f"threshold, {len(edits)} diff edit(s), {len(notes)} evidence-only note(s)",
        file=err,
    )
    if args.evidence_out:
        dest = resolve_within(roots, args.evidence_out)
        if dest == table or (dest.exists() and os.path.samefile(dest, table)):
            raise PathEscapeError(
                "--evidence-out must not be the table file (this script never writes it)"
            )
        dest.write_text(md, encoding="utf-8")
    else:
        err.write(md)
    return 0


SELF_TEST_TABLE = """intro
| Change class | Required gates (in order) |
|---|---|
| Pure bugfix with identified root cause | `regression-check` → `test-scaffold` (if no coverage) → `changelog` → `/mr` |
| Backend-only feature (new endpoint or model) | `architect` → batched pre-MR gate cluster (see below) → `/mr` |
| Settings sub-page wiring (form) | `ux-review` → `rbac-check` + `security-review` + `regression-check` → `/mr` |

outro
"""


def self_test():
    ok = True

    def check(cond, msg):
        nonlocal ok
        if not cond:
            print(
                f"kaizen-fastpath-proposal --self-test: FAIL - {msg}", file=sys.stderr
            )
            ok = False

    def pair(streak, evidence=()):
        return {
            "streak": streak,
            "runs": streak + 2,
            "zero": streak,
            "positive": 2,
            "evidence": list(evidence),
        }

    st = watch.empty_state()
    st["pairs"] = {
        "regression-check|fix": pair(10, range(1, 11)),
        "rbac-check|feat": pair(12, range(20, 30)),
        "ux-review|feat": pair(9),
        "perf-check|chore": pair(11),
    }
    new, edits, notes = build(st, set(), SELF_TEST_TABLE)
    check(
        "| Pure bugfix with identified root cause | `test-scaffold` (if no coverage)"
        in new,
        "fix row must lose regression-check",
    )
    check(
        "`ux-review` → `security-review` + `regression-check` → `/mr`" in new,
        "plus-joined item must be removed alone",
    )
    check(
        "`ux-review`" in new and len(edits) == 2,
        "below-threshold pair must not be edited",
    )
    check(
        any("pre-MR gate cluster" in why for _, _, _, why in notes),
        "cluster-only gate must be a note, not an edit",
    )
    check(
        any(s == "chore" and "nothing to narrow" in w for _, s, _, w in notes),
        "unmatched row must be a note",
    )
    st["pairs"]["regression-check|other"] = pair(10)
    _, _, n2 = build(st, set(), SELF_TEST_TABLE)
    check(
        any(s == "other" and "maps to no table row" in w for _, s, _, w in n2),
        "unmapped shape must be a note",
    )
    del st["pairs"]["regression-check|other"]
    md = evidence_md(st, edits, notes, "state.json")
    check(
        "!1, !2" in md and "| `regression-check` | `fix` | 10 |" in md,
        "evidence must cite streak and MRs",
    )
    same, e2, _ = build(
        st,
        {"regression-check:fast-path-candidate", "rbac-check:fast-path-candidate"},
        SELF_TEST_TABLE,
    )
    check(same == SELF_TEST_TABLE and not e2, "declined gates must not be proposed")
    with tempfile.TemporaryDirectory() as d:
        tbl = Path(d) / "CLAUDE.md"
        tbl.write_text(SELF_TEST_TABLE, encoding="utf-8")
        before = tbl.read_text(encoding="utf-8")
        sp = Path(d) / "state.json"
        sp.write_text(json.dumps(st), encoding="utf-8")
        ns = argparse.Namespace(
            state=str(sp),
            declined=str(Path(d) / "none.json"),
            table=str(tbl),
            evidence_out=str(Path(d) / "ev.md"),
        )
        buf, err = io.StringIO(), io.StringIO()
        rc = run(ns, out=buf, err=err)
        check(
            rc == 0 and tbl.read_text(encoding="utf-8") == before,
            "run must never write the table",
        )
        check(
            "-| Pure bugfix" in buf.getvalue() and "+| Pure bugfix" in buf.getvalue(),
            "run must emit a unified diff",
        )
        check("2 diff edit(s)" in err.getvalue(), "run must print a one-line summary")
        link = Path(d) / "hardlink.md"
        os.link(tbl, link)
        ns.evidence_out = str(link)
        try:
            run(ns, out=io.StringIO(), err=io.StringIO())
            check(False, "a hard link to the table must be refused as --evidence-out")
        except PathEscapeError:
            pass
        check(
            tbl.read_text(encoding="utf-8") == before,
            "hard link must not let the table be written",
        )

    # The state file's threshold wins over the module constant.
    lo = watch.empty_state()
    lo["threshold"] = 5
    lo["pairs"] = {"regression-check|fix": pair(7)}
    _, e3, _ = build(lo, set(), SELF_TEST_TABLE)
    check(
        len(e3) == 1 and watch.THRESHOLD > 7,
        "state threshold (5) must win over the constant",
    )
    lo["threshold"] = watch.THRESHOLD + 5
    lo["pairs"] = {"regression-check|fix": pair(watch.THRESHOLD + 1)}
    check(
        not build(lo, set(), SELF_TEST_TABLE)[1],
        "a higher state threshold must suppress a lower streak",
    )

    # Splitting: non-canonical spacing, `->`, and an arrow inside a qualifier.
    cell = "`a` (if x -> y)  ->`b`+  `c`→`d`"
    check(
        remove_gate(cell, "b") == "`a` (if x -> y)  ->`c`→`d`",
        "plus-group item must drop its + sep",
    )
    check(
        remove_gate(cell, "a") == "`b`+  `c`→`d`",
        "first item must drop the following arrow",
    )
    check(remove_gate(cell, "y") is None, "a word inside a qualifier is not an item")
    for bad, gate in (
        ("`a` (unclosed → `b`", "b"),
        ("run `b` only if → `c`", "b"),
        ("`b`", "b"),
    ):
        try:
            remove_gate(bad, gate)
            check(False, f"unsafe cell must refuse: {bad}")
        except Unsafe:
            pass

    # Removing a gate from every row that runs it is a deletion: note, no diff.
    only = watch.empty_state()
    only["pairs"] = {"ux-review|feat": pair(10)}
    same2, e4, n4 = build(only, set(), SELF_TEST_TABLE)
    check(same2 == SELF_TEST_TABLE and not e4, "a gate deletion must not be diffed")
    check(
        any("gate deletion" in w for *_, w in n4),
        "a gate deletion must be an evidence note",
    )
    check(
        "Gate reference row" in md,
        "evidence must carry the collateral-review checklist",
    )
    if ok:
        print(
            "kaizen-fastpath-proposal --self-test: OK - narrows named gates at the state threshold; "
            "notes cluster/unmapped/unsafe/deletion; honors declined; never writes the table."
        )
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--state",
        default=watch.DEFAULT_STATE,
        help="yield state from kaizen_yield_watch (#1144)",
    )
    ap.add_argument("--declined", default=watch.DEFAULT_DECLINED)
    ap.add_argument(
        "--table",
        help="file holding the fast-path table (default: search repo CLAUDE.md files)",
    )
    ap.add_argument(
        "--evidence-out",
        metavar="FILE",
        help="write the MR evidence section here (default: stderr)",
    )
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    try:
        sys.exit(run(args))
    except PathEscapeError as exc:
        roots = ", ".join(str(r) for r in cli_roots())
        print(
            f"kaizen-fastpath-proposal: {exc}\n  allowed roots: {roots} (and {HOME_CLAUDE} for --table).\n"
            '  On macOS the temp root is $TMPDIR, not /tmp: write to "${TMPDIR:-/tmp}/..." or under the repo.',
            file=sys.stderr,
        )
        sys.exit(2)
    except (TableNotFound, OSError, ValueError, KeyError) as exc:
        print(f"kaizen-fastpath-proposal: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
