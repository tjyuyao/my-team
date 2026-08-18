"""Regression test for KANBAN board invariants.

Imports the shared checker (KANBAN/kanban_lint.py) and asserts the board
has zero violations. This turns the format contract into a CI gate: any
commit that leaves a malformed board file (bad filename, missing
frontmatter, wrong kind, dangling reference, …) fails the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "KANBAN"))

import kanban_lint  # noqa: E402


def test_kanban_board_is_valid():
    root = Path(__file__).resolve().parent.parent / "KANBAN"
    violations = kanban_lint.check_board(root)
    assert violations == [], "\n".join(violations)
