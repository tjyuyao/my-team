"""SQLite-backed persistence for Simulation state (P3-11).

The simulation state is a set of subsystem component blobs (JSON).
Saving writes ALL components in ONE SQLite transaction: either the
previous state remains or the new state is complete — never partial.
This gives the crash-recovery property (pause → shutdown → restart →
resume) at tick boundaries.

Schema:
  meta(key TEXT PRIMARY KEY, value TEXT)     — schema_version
  state(component TEXT PRIMARY KEY, payload TEXT)  — JSON blob per subsystem

Usage:
  store = SimulationStore("sim.db")
  store.save({"config": {...}, "tasks": {...}, ...})
  state = store.load()          # dict of components, or None if empty
  store.wipe()
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class SimulationStore:
    """Atomic key-value persistence for simulation component blobs."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, components: dict[str, Any]) -> None:
        """Persist all components in one transaction (all-or-nothing)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS state "
                "(component TEXT PRIMARY KEY, payload TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            for component, payload in components.items():
                conn.execute(
                    "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                    (component, json.dumps(payload)),
                )
            # conn context manager commits; a crash before commit leaves
            # the previous state intact.

    def load(self) -> dict[str, Any] | None:
        """Read all components. Returns None when no state has been saved."""
        if not self._path.exists():
            return None
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute("SELECT component, payload FROM state").fetchall()
        if not rows:
            return None
        return {
            component: json.loads(payload)
            for component, payload in rows
        }

    def schema_version(self) -> int | None:
        """Read the persisted schema version (None if DB is empty)."""
        if not self._path.exists():
            return None
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row[0]) if row else None

    def wipe(self) -> None:
        """Delete the database file."""
        self._path.unlink(missing_ok=True)
