"""Regression tests for KANBAN/enforce_filename_dates.py.

Locks the two behaviors that matter for R2 (filename date == git commit
date):

1. **Clean files are never renamed** — even when their mtime is older
   than their filename date.  The legacy mtime-based script renamed such
   files backwards to the old edit day and broke R2; this must not
   regress.
2. **Files with uncommitted changes** (dirty or untracked) get renamed
   to today — their next commit date will be today.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

# tests/integration → tests → repo root → KANBAN
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "KANBAN"),
)

import enforce_filename_dates as efd  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def _make_board(tmp_path: Path) -> tuple[Path, Path]:
    """Create a temp git repo with a KANBAN/TODO column, return (repo, kanban)."""
    repo = tmp_path / "repo"
    kanban = repo / "KANBAN"
    (kanban / "TODO").mkdir(parents=True)
    assert _git(repo, "init", "-q").returncode == 0
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    return repo, kanban


def _commit_all(repo: Path, msg: str) -> None:
    assert _git(repo, "add", "-A").returncode == 0
    res = _git(repo, "commit", "-q", "-m", msg)
    assert res.returncode == 0, res.stderr


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def test_clean_committed_file_never_renamed_backwards(tmp_path):
    """Clean file with an old mtime must NOT be renamed (regression lock)."""
    repo, kanban = _make_board(tmp_path)
    f = kanban / "TODO" / "2026-08-24-foo.md"
    f.write_text("---\nkind: task\n---\n")
    _commit_all(repo, "add foo")

    # Backdate mtime to an earlier day.  The legacy mtime script would
    # "fix" the filename back to the old edit day and break R2.
    old = dt.datetime(2026, 8, 18, 12, 0, 0).timestamp()
    os.utime(f, (old, old))

    assert efd.main(["--check", "--root", str(kanban)]) == 0
    assert f.exists()  # filename untouched


def test_dirty_file_renamed_to_today(tmp_path):
    """File with uncommitted edits gets renamed to today."""
    repo, kanban = _make_board(tmp_path)
    f = kanban / "TODO" / "2026-08-24-bar.md"
    f.write_text("---\nkind: task\n---\n")
    _commit_all(repo, "add bar")

    f.write_text("---\nkind: task\n---\n# changed\n")  # uncommitted edit

    assert efd.main(["--check", "--root", str(kanban)]) == 1
    efd.main(["--root", str(kanban)])

    renamed = kanban / "TODO" / f"{_today()}-bar.md"
    assert renamed.exists()
    assert not f.exists()  # old name gone


def test_untracked_file_renamed_to_today(tmp_path):
    """Never-committed (untracked) file gets renamed to today."""
    repo, kanban = _make_board(tmp_path)
    f = kanban / "TODO" / "2026-08-24-baz.md"
    f.write_text("---\nkind: task\n---\n")  # untracked, never committed

    efd.main(["--root", str(kanban)])

    renamed = kanban / "TODO" / f"{_today()}-baz.md"
    assert renamed.exists()
    assert not f.exists()
