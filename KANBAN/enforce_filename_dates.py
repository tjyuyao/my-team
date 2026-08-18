#!/usr/bin/env python3
"""Enforce KANBAN filename date prefixes based on last-modified time.

KANBAN files must be named ``YYYY-MM-DD-{topic}.md``, where the date is
the file's last-modified date.  This script scans the standard KANBAN
columns and renames any ``*.md`` file whose date prefix does not match
its filesystem last-modified date.

Usage:
  python3 KANBAN/enforce_filename_dates.py            # apply fixes
  python3 KANBAN/enforce_filename_dates.py --check    # report only
  python3 KANBAN/enforce_filename_dates.py --root /path/to/KANBAN
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
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


def expected_name(path: Path, mtime_date: str) -> str:
    """Return the filename *path* should have for the given last-modified date."""
    name = path.name
    if has_date_prefix(name):
        topic = name[11:]  # drop the old YYYY-MM-DD- prefix
    else:
        topic = name  # no prefix yet; prepend one
    return f"{mtime_date}-{topic}"


def mtime_date(path: Path) -> str:
    """Return the file's last-modified date as ``YYYY-MM-DD`` in local time."""
    return dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime(DATE_PREFIX_FORMAT)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rename KANBAN *.md files so their date prefix matches their last-modified date."
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
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    renamed = 0
    problems = 0

    for path in iter_board_files(root):
        try:
            date = mtime_date(path)
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
