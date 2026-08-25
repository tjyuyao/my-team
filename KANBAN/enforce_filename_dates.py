#!/usr/bin/env python3
"""Enforce KANBAN filename date prefixes based on git uncommitted state.

KANBAN files must be named ``YYYY-MM-DD-{topic}.md``, where the date is
the file's last commit date (R2 in kanban_lint.py).  This script is run
BEFORE committing: it renames the date prefix to **today** for every
board file that has uncommitted changes (git diff HEAD non-empty, or
untracked) — those files will be committed today, so today becomes their
commit date and the rename keeps R2 satisfied.

Clean files (no uncommitted changes) are left untouched: their prefix
already equals their last commit date.  The legacy mtime-based script
renamed those backwards whenever an edit day differed from the commit
day, which broke R2 — this mode is gone.

Commit the renames the same day you run the script: R2 compares the
filename prefix against the git commit date, so a rename committed on a
later day is flagged.

Outside a git repository the script degrades to the legacy mtime-based
mode with a warning (R2 cannot validate dates without git anyway).

Usage:
  python3 KANBAN/enforce_filename_dates.py            # apply fixes
  python3 KANBAN/enforce_filename_dates.py --check    # report only
  python3 KANBAN/enforce_filename_dates.py --root /path/to/KANBAN
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
from pathlib import Path

DATE_PREFIX_FORMAT = "%Y-%m-%d"
KANBAN_COLUMNS = (
    "PLAN",
    "OPEN_ISSUE",
    "CLOSED_ISSUE",
    "TODO",
    "IN_PROGRESS",
    "DONE",
    "MILESTONE",
)


def iter_board_files(root: Path):
    """Yield markdown files in the standard KANBAN columns.

    ``README.md`` files are exempt from the date-prefix rule.
    """
    for column in KANBAN_COLUMNS:
        column_dir = root / column
        if not column_dir.is_dir():
            continue
        for path in sorted(column_dir.glob("*.md")):
            if path.name == "README.md":
                continue
            yield path


def has_date_prefix(name: str) -> bool:
    """Return True if *name* starts with a valid ``YYYY-MM-DD-`` prefix."""
    if len(name) < len("YYYY-MM-DD-") or name[10] != "-":
        return False
    try:
        dt.datetime.strptime(name[:10], DATE_PREFIX_FORMAT)
    except ValueError:
        return False
    return True


def expected_name(path: Path, date: str) -> str:
    """Return the filename *path* should have for the given date."""
    name = path.name
    if has_date_prefix(name):
        topic = name[11:]  # drop the old YYYY-MM-DD- prefix
    else:
        topic = name  # no prefix yet; prepend one
    return f"{date}-{topic}"


def find_git_root(start: Path) -> Path | None:
    """Return the git repository root containing *start*, or None."""
    res = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    return Path(res.stdout.strip())


def _is_untracked(path: Path, repo_root: Path) -> bool:
    """True if *path* is not tracked by git (a new, never-committed file)."""
    rel = path.relative_to(repo_root)
    res = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", str(rel)],
        capture_output=True, text=True,
    )
    return res.returncode != 0


def has_uncommitted_changes(path: Path, repo_root: Path) -> bool:
    """True if the file will change in the next commit (dirty or untracked).

    ``git diff HEAD`` covers both staged and unstaged edits relative to
    the last commit; untracked files are detected separately.
    """
    if _is_untracked(path, repo_root):
        return True
    rel = path.relative_to(repo_root)
    res = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "HEAD", "--name-only", "--", str(rel)],
        capture_output=True, text=True,
    )
    return bool(res.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rename KANBAN *.md files so their date prefix matches their next "
            "commit date (today) — only for files with uncommitted changes."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="KANBAN directory to scan (default: directory containing this script)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report files that would be renamed; exit 1 if any are found",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    repo_root = find_git_root(root)
    if repo_root is None:
        print(
            "WARNING: not inside a git repository; R2 cannot validate dates. "
            "Falling back to legacy mtime-based renaming."
        )
        git_mode = False
    else:
        git_mode = True

    renamed = 0
    problems = 0

    for path in iter_board_files(root):
        if git_mode:
            # Clean files already carry their last commit date; renaming
            # them (e.g. back to an old mtime) would break R2.
            if not has_uncommitted_changes(path, repo_root):
                continue
            date = dt.date.today().strftime(DATE_PREFIX_FORMAT)
        else:
            try:
                date = dt.datetime.fromtimestamp(
                    os.path.getmtime(path),
                ).strftime(DATE_PREFIX_FORMAT)
            except OSError as exc:
                problems += 1
                print(f"SKIP {path}: cannot read mtime: {exc}")
                continue

        new_name = expected_name(path, date)
        if new_name == path.name:
            continue

        new_path = path.with_name(new_name)
        if new_path.exists():
            problems += 1
            print(f"SKIP {path}: target already exists: {new_path}")
            continue

        print(f"{'would rename' if args.check else 'rename'}: {path.name} -> {new_name}")
        if not args.check:
            path.replace(new_path)
        renamed += 1

    if args.check:
        if renamed or problems:
            print(
                f"\n{renamed} file(s) need renaming, {problems} problem(s); "
                "run without --check to apply."
            )
            return 1
        print("\n0 file(s) need renaming")
        return 0

    print(f"\n{renamed} file(s) renamed; board filename dates are enforced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
