#!/usr/bin/env python3
"""KANBAN board invariant checker — shared library (stdlib + git).

Single source of truth for the board structure contract, imported by both
the CLI tooling and the pytest regression suite so the two can never
disagree.

Every board ``*.md`` (except README.md) must carry a YAML frontmatter
block as its very first lines::

    ---
    kind: task          # required: plan | issue | task | report
    status: completed   # task: completed|rejected; plan: active|archived;
                        # issue: open|closed (required on terminal columns)
    phase: v0.10        # optional, free string
    source: SPEC §6.2   # optional, free string
    priority: high      # optional: high | medium | low
    r7_exempt: a,b      # optional: comma-separated topics allowed to dangle
    ---

Contract rules (see KANBAN/README.md):
  R1  filename == ``YYYY-MM-DD-{lowercase-hyphen-topic}.md``
      (archived plans append ``.archived`` before ``.md``)
  R2  date prefix == file's last commit date (git log %cs = committer
      date; rebase/amend updates it; mtime fallback for untracked files /
      non-git contexts; skipped with a clear message in shallow clones —
      needs full history, CI checkout must use fetch-depth: 0)
  R3  frontmatter present; ``kind`` valid and matches its column
  R4  PLAN version ids (``vX.Y.Z``) unique across the column
  R5  column/kind coherence: DONE=tasks only, CLOSED_ISSUE=issues only,
      MILESTONE=reports only
  R6  ``status`` valid for its kind; required on terminal columns
  R7  cross-references resolve to a real board topic (by topic, not date)
  R8  pyproject.toml version >= highest completed board version
      (completed = MILESTONE reports + archived PLANs)
  R9  every active (non-archived) PLAN version >= pyproject.toml version
      (once a version has shipped, its plan must be archived)
  R10 a MILESTONE-reported version's plan must be archived (not active)
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
from pathlib import Path

DATE_FMT = "%Y-%m-%d"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
# Topic allows lowercase/digits/dash/dot — dot is needed for version ids
# (v0.7.0) and the .archived suffix.
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9.-]*\.md$")

COLUMNS = (
    "PLAN",
    "OPEN_ISSUE",
    "CLOSED_ISSUE",
    "TODO",
    "IN_PROGRESS",
    "DONE",
    "MILESTONE",
)

COLUMN_KIND = {
    "PLAN": "plan",
    "OPEN_ISSUE": "issue",
    "CLOSED_ISSUE": "issue",
    "TODO": "task",
    "IN_PROGRESS": "task",
    "DONE": "task",
    "MILESTONE": "report",
}

KINDS = frozenset({"plan", "issue", "task", "report"})

# status values legal per kind; empty set = no status for that kind.
STATUS_BY_KIND = {
    "plan": {"active", "archived"},
    "issue": {"open", "closed"},
    "task": {"completed", "rejected"},
    "report": set(),
}

# Columns where a status is REQUIRED (and its allowed values).
TERMINAL_STATUS = {
    "DONE": {"completed", "rejected"},
    "CLOSED_ISSUE": {"closed"},
}

PRIORITIES = {"high", "medium", "low"}

# References that legitimately point outside the board.
# SPEC 双版制（2026-08-24）：SPEC.md = 设计权威（新编号）；
# SPEC.v0.11.legacy.md = 旧骨架备份（v0.11 及更早 KANBAN 文件引旧编号）。
REF_EXEMPT = {"README", "SPEC", "SPEC.v0.8.legacy", "SPEC.v0.11.legacy"}


def iter_board_files(root: Path):
    """Yield every board ``*.md`` (README.md exempt)."""
    for column in COLUMNS:
        column_dir = root / column
        if not column_dir.is_dir():
            continue
        for path in sorted(column_dir.glob("*.md")):
            if path.name == "README.md":
                continue
            yield path


def mtime_date(path: Path) -> str:
    """File's last-modified date as ``YYYY-MM-DD`` (local time)."""
    return dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime(DATE_FMT)


def last_commit_date(path: Path) -> str | None:
    """Date (``YYYY-MM-DD``) of the most recent commit touching ``path``.

    Uses ``git log -1 --format=%cs``. Unlike mtime, the commit date is
    versioned, so the check behaves identically on any checkout (CI, other
    machines). Returns ``None`` when git is unavailable or the file has
    never been committed (untracked/new).
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", path.name],
            cwd=path.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", out):
        return out
    return None


_SHALLOW: bool | None = None


def _repo_is_shallow(root: Path) -> bool:
    """True when the repo is a shallow clone (R2 cannot be verified).

    In a shallow clone ``git log -1 -- <path>`` degrades to the tip commit
    for every path, so commit-date checks would produce mass false
    violations. Callers should skip per-file R2 and report this instead.
    """
    global _SHALLOW
    if _SHALLOW is None:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            _SHALLOW = proc.returncode == 0 and proc.stdout.strip() == "true"
        except (OSError, subprocess.SubprocessError):
            _SHALLOW = False
    return _SHALLOW


def topic_of(name: str) -> str:
    """Canonical topic: strip ``.md``, ``.archived`` and the date prefix.

    Cross-references are matched on this value, so a reference stays valid
    across date renames and plan archiving.
    """
    name = name[:-3] if name.endswith(".md") else name
    if name.endswith(".archived"):
        name = name[: -len(".archived")]
    m = DATE_RE.match(name)
    if m:
        name = name[m.end():]
    return name


def parse_frontmatter(path: Path) -> dict[str, str] | None:
    """Parse a ``---``-delimited scalar frontmatter block, or None.

    Scalar-only: each line is ``key: value``; values may be double- or
    single-quoted. Nested YAML is intentionally unsupported.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[4:end]
    fm: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        fm[key.strip()] = val
    return fm


def extract_refs(text: str) -> list[str]:
    """Collect file references worth checking.

    Only two shapes count as a cross-reference: a ``Source:`` field
    mention, or a backtick that looks like a board path (contains ``/``
    or a date prefix). Bare ``foo.md`` in prose (e.g. a test scenario
    that "writes report.md") is NOT a reference.
    """
    refs: list[str] = []
    for m in re.finditer(r"\*\*Source:\*\*(.+)$", text, re.MULTILINE):
        refs.extend(re.findall(r"[\w./-]+\.md", m.group(1)))
    for m in re.finditer(r"`([^`]*\.md)`", text):
        ref = m.group(1)
        base = ref.split("/")[-1]
        if "/" in ref or DATE_RE.match(base):
            refs.append(ref)
    return refs


def _parse_ver(s: str) -> tuple[int, int, int] | None:
    """Parse a ``v?X.Y.Z`` version string into a comparable tuple."""
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _pyproject_version(root: Path) -> tuple[int, int, int] | None:
    """Read the static ``version = "X.Y.Z"`` from the project pyproject.toml."""
    py = root.parent / "pyproject.toml"
    if not py.is_file():
        return None
    try:
        text = py.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r'^\s*version\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            return _parse_ver(m.group(1))
    return None


def _parse_exempt(raw: str) -> set[str]:
    """Parse the comma-separated ``r7_exempt`` topic list."""
    return {t.strip() for t in raw.split(",") if t.strip()}


def check_board(root: Path) -> list[str]:
    """Return a list of human-readable violations (empty == board is valid)."""
    violations: list[str] = []
    files = list(iter_board_files(root))
    topics = {topic_of(p.name) for p in files}
    fms: dict[Path, dict[str, str]] = {}

    # R2 needs full git history; a shallow clone cannot verify date
    # invariants (git log -1 -- <path> degrades to the tip commit).
    shallow = _repo_is_shallow(root)
    if shallow:
        violations.append(
            "R2: 仓库为浅克隆(shallow), 无完整历史, 无法校验日期不变量 —— "
            "git fetch --unshallow (CI: actions/checkout 需 fetch-depth: 0)"
        )

    for p in files:
        rel = str(p.relative_to(root))

        # R1 filename format
        if not FILENAME_RE.match(p.name):
            violations.append(
                f"{rel}: R1 filename must be YYYY-MM-DD-lowercase-topic.md"
            )
            continue

        # R2 date prefix == last commit date (mtime fallback for untracked)
        if not shallow:
            date = p.name[:10]
            actual = last_commit_date(p)
            if actual is None:
                actual = mtime_date(p)
            if date != actual:
                violations.append(f"{rel}: R2 date {date} != {actual}")

        # R3 frontmatter present + kind valid & matches column
        fm = parse_frontmatter(p)
        if fm is None:
            violations.append(f"{rel}: R3 missing YAML frontmatter (--- ... ---)")
            continue
        fms[p] = fm
        kind = fm.get("kind")
        expected = COLUMN_KIND[p.parent.name]
        if kind is None or kind not in KINDS:
            violations.append(f"{rel}: R3 frontmatter kind missing/invalid: {kind!r}")
            continue
        if kind != expected:
            violations.append(
                f"{rel}: R3 kind={kind!r} but column {p.parent.name} "
                f"requires {expected!r}"
            )

        # R5 column/kind coherence (implied by R3 except DONE-subset checks)
        # DONE must be task-only — R3 already enforces kind==task there.

        # R6 status validity + terminal requirement
        status = fm.get("status")
        if status is not None and status not in STATUS_BY_KIND.get(kind, set()):
            violations.append(
                f"{rel}: R6 invalid status {status!r} for kind={kind!r}"
            )
        column = p.parent.name
        if column in TERMINAL_STATUS and status is None:
            violations.append(
                f"{rel}: R6 column {column} requires status in "
                f"{sorted(TERMINAL_STATUS[column])}"
            )
        if ".archived" in p.name and status != "archived":
            violations.append(
                f"{rel}: R6 .archived plan requires status=archived"
            )

        # priority enum (optional)
        prio = fm.get("priority")
        if prio is not None and prio not in PRIORITIES:
            violations.append(f"{rel}: R6 invalid priority {prio!r}")

    # R4 PLAN version uniqueness
    plan_versions: dict[str, list[str]] = {}
    for p in files:
        if p.parent.name == "PLAN":
            m = re.search(r"v\d+\.\d+\.\d+", topic_of(p.name))
            if m:
                plan_versions.setdefault(m.group(0), []).append(p.name)
    for ver, names in plan_versions.items():
        if len(names) > 1:
            violations.append(f"PLAN: R4 duplicate version {ver}: {names}")

    # R7 dangling references (only well-formed topics count as refs)
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(p.relative_to(root))
        exempt = _parse_exempt(fms.get(p, {}).get("r7_exempt", ""))
        for ref in extract_refs(text):
            t = topic_of(ref.split("/")[-1])
            if not TOPIC_RE.match(t):
                continue  # spaces / wildcards / shell examples — not a ref
            if t in REF_EXEMPT or t in exempt:
                continue
            if t not in topics:
                violations.append(f"{rel}: R7 dangling ref {ref!r} (topic {t!r})")

    # R8 pyproject version >= highest completed board version
    py_ver = _pyproject_version(root)
    completed: list[tuple[int, int, int]] = []
    for p in files:
        if p.parent.name == "MILESTONE":
            v = _parse_ver(p.name)
            if v:
                completed.append(v)
        elif p.parent.name == "PLAN" and ".archived" in p.name:
            v = _parse_ver(p.name)
            if v:
                completed.append(v)
    if py_ver is None:
        violations.append(
            "R8 cannot determine project version (pyproject.toml version = ...)"
        )
    elif completed:
        max_done = max(completed)
        if py_ver < max_done:
            violations.append(
                f"R8 pyproject version {'.'.join(map(str, py_ver))} < completed "
                f"board version {'.'.join(map(str, max_done))}"
            )

    # R9 a shipped version's plan must be archived (not still active)
    if py_ver is not None:
        for p in files:
            if p.parent.name == "PLAN" and ".archived" not in p.name:
                v = _parse_ver(p.name)
                if v is not None and v < py_ver:
                    violations.append(
                        f"{str(p.relative_to(root))}: R9 active plan version "
                        f"{'.'.join(map(str, v))} < pyproject "
                        f"{'.'.join(map(str, py_ver))} — must be archived"
                    )

    # R10 a version with a MILESTONE report must not have an active plan
    milestone_vers = {
        v
        for p in files
        if p.parent.name == "MILESTONE"
        for v in [_parse_ver(p.name)]
        if v is not None
    }
    for p in files:
        if p.parent.name == "PLAN" and ".archived" not in p.name:
            v = _parse_ver(p.name)
            if v is not None and v in milestone_vers:
                violations.append(
                    f"{str(p.relative_to(root))}: R10 plan version "
                    f"{'.'.join(map(str, v))} has a MILESTONE report but is "
                    f"still active — must be archived"
                )

    return violations


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Check KANBAN board invariants (R1..R7)."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="KANBAN directory to scan",
    )
    args = parser.parse_args()
    violations = check_board(args.root.resolve())
    for v in violations:
        print(v)
    print(f"\n{len(violations)} violation(s).")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
