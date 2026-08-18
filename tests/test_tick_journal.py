"""Tests for T4: Unified TickJournal.

Covers: TickJournal unit tests, AuditLog → Journal delegation,
simulation integration (commit + rollback), persistence round-trip.

Date: 2026-08-18
"""

from __future__ import annotations

from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType, AuditLog
from my_team.journal import (
    TickJournal,
    TickRecordStatus,
)
from my_team.simulation import Simulation
from my_team.transaction import EffectType


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls"],
                "can_delegate": False,
                "metadata": {"bootstrap": True},
            },
        ],
    })


# ---------------------------------------------------------------------------
# TickJournal unit tests
# ---------------------------------------------------------------------------

class TestTickJournalUnit:
    def test_start_tick_creates_record(self):
        journal = TickJournal()
        record = journal.start_tick(tick=0, epoch=0)
        assert record.tick == 0
        assert record.epoch == 0
        assert record.status == TickRecordStatus.COMMITTED
        assert journal.current_record is record

    def test_finalize_committed(self):
        journal = TickJournal()
        journal.start_tick(0, 0)
        record = journal.finalize(TickRecordStatus.COMMITTED)
        assert record.status == TickRecordStatus.COMMITTED
        assert journal.current_record is None
        assert len(journal) == 1

    def test_finalize_aborted(self):
        journal = TickJournal()
        journal.start_tick(0, 0)
        record = journal.finalize(TickRecordStatus.ABORTED, error="test error")
        assert record.status == TickRecordStatus.ABORTED
        assert record.error == "test error"

    def test_multiple_ticks(self):
        journal = TickJournal()
        for t in range(3):
            journal.start_tick(t, epoch=t)
            journal.finalize()
        assert len(journal) == 3
        assert [r.tick for r in journal.records] == [0, 1, 2]

    def test_for_tick(self):
        journal = TickJournal()
        journal.start_tick(5, 0)
        journal.finalize()
        assert journal.for_tick(5) is not None
        assert journal.for_tick(4) is None

    def test_last_n(self):
        journal = TickJournal()
        for t in range(5):
            journal.start_tick(t, 0)
            journal.finalize()
        assert len(journal.last(2)) == 2
        assert journal.last(2)[0].tick == 3

    def test_finalize_without_start_raises(self):
        journal = TickJournal()
        import pytest
        with pytest.raises(RuntimeError, match="No active tick"):
            journal.finalize()


# ---------------------------------------------------------------------------
# AuditLog → Journal delegation
# ---------------------------------------------------------------------------

class TestAuditLogJournalDelegation:
    def test_record_writes_to_journal(self):
        journal = TickJournal()
        journal.start_tick(0, 0)
        log = AuditLog(journal=journal)
        log.record(AuditEventType.TICK_COMPLETE, tick=0)
        record = journal.current_record
        assert record is not None
        assert len(record.audit_events) == 1
        assert record.audit_events[0].event_type == AuditEventType.TICK_COMPLETE

    def test_record_without_journal_still_works(self):
        log = AuditLog(journal=None)
        entry = log.record(AuditEventType.TICK_COMPLETE, tick=0)
        assert entry.event_type == AuditEventType.TICK_COMPLETE
        assert len(log) == 1

    def test_record_outside_tick_skips_journal(self):
        journal = TickJournal()
        # No start_tick — current_record is None
        log = AuditLog(journal=journal)
        log.record(AuditEventType.TICK_COMPLETE, tick=0)
        # Entry goes to AuditLog but not to any TickRecord
        assert len(log) == 1
        assert len(journal) == 0


# ---------------------------------------------------------------------------
# Simulation integration
# ---------------------------------------------------------------------------

class TestTickJournalIntegration:
    def test_run_tick_creates_journal_record(self):
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        assert len(sim._journal) == 1
        record = sim._journal.records[0]
        assert record.tick == 0
        assert record.status == TickRecordStatus.COMMITTED

    def test_journal_captures_audit_events(self):
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        record = sim._journal.records[0]
        # At minimum: AGENT_CREATED (init) + TICK_COMPLETE
        event_types = [e.event_type for e in record.audit_events]
        assert AuditEventType.TICK_COMPLETE in event_types

    def test_journal_snapshot_hash(self):
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        record = sim._journal.records[0]
        assert record.snapshot_hash != ""

    def test_rollback_creates_aborted_record(self):
        sim = Simulation(agent_tree=_make_tree())
        # Stage a failing effect to trigger rollback
        sim.task_tree.create(
            task_id="task.dup", title="T",
            creator_agent_id="agent.root", owner_agent_id="agent.root",
        )
        sim._transaction_buffer.stage(
            EffectType.TASK_CREATE, "agent.root", "task.dup",
            data={"task_id": "task.dup", "title": "Dup",
                  "creator_agent_id": "agent.root",
                  "owner_agent_id": "agent.root"},
        )
        sim.run_tick()
        record = sim._journal.records[0]
        assert record.status == TickRecordStatus.ABORTED
        assert record.error is not None

    def test_journal_accumulates_across_ticks(self):
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        sim.run_tick()
        sim.run_tick()
        assert len(sim._journal) == 3
        assert [r.tick for r in sim._journal.records] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

class TestTickJournalPersistence:
    def test_journal_survives_save_load(self, tmp_path):
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        sim.run_tick()
        path = tmp_path / "sim.db"
        sim.save_to(path)

        sim2 = Simulation.load_from(path)
        assert len(sim2._journal) == 2
        assert sim2._journal.records[0].tick == 0
        assert sim2._journal.records[1].tick == 1

    def test_journal_committed_status_survives(self, tmp_path):
        sim = Simulation(agent_tree=_make_tree())
        sim.run_tick()
        path = tmp_path / "sim.db"
        sim.save_to(path)

        sim2 = Simulation.load_from(path)
        assert sim2._journal.records[0].status == TickRecordStatus.COMMITTED

    def test_backward_compat_no_journal(self, tmp_path):
        """Old databases without tick_journal key still load correctly."""
        import json
        import sqlite3

        from my_team.persistence import SCHEMA_VERSION

        # Create a legacy-format database without tick_journal
        path = tmp_path / "legacy.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE state (component TEXT PRIMARY KEY, payload TEXT)")
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            # Minimal state with just config and audit
            state = {
                "config": {
                    "tick_duration_value": 10,
                    "tick_duration_unit": "seconds",
                    "simulation_time_per_tick_value": 1,
                    "simulation_time_per_tick_unit": "hour",
                    "start_paused": False,
                    "deterministic_mode": True,
                    "max_concurrent_llm_requests": 4,
                    "max_concurrent_tool_requests": 8,
                    "email_delivery_latency_ticks": 1,
                    "max_retries": 3,
                    "private_storage_limit_mb": 512,
                    "execution": {
                        "max_llm_calls_per_activation": 1,
                        "max_tool_calls_per_activation": 8,
                        "max_action_budget": 32,
                    },
                },
                "agent_tree": [
                    {
                        "agent_id": "agent.root",
                        "display_name": "Root",
                        "role": "root",
                        "parent_id": None,
                        "children": [],
                        "tools": ["read"],
                        "can_delegate": False,
                        "metadata": {"bootstrap": True},
                    },
                ],
                "tick_engine": {"current_tick": 0, "state": "created"},
                "state_epoch": 0,
                "private_store_base_path": "private",
                "tasks": {"tasks": {}, "parent_map": {}, "children_map": {}, "owner_map": {}},
                "emails": {"all": {}, "pending": [], "mailboxes": {}},
                "scheduler": {
                    "wake_conditions": {}, "events": [],
                    "activation_history": [], "activation_counter": 0,
                },
                "outbox": {"entries": [], "max_retries": 3},
                "pending_ops": {"operations": [], "seen_requests": {}},
                "kb": {"resources": [], "versions": [], "permissions": []},
                "locks": {"locks": [], "lock_counter": 0},
                "audit": {"entries": [], "next_event_id": 0},
                "file_ops_audit": [],
                "agent_states": {},
            }
            conn.execute(
                "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                ("config", json.dumps(state["config"])),
            )
            conn.execute(
                "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                ("agent_tree", json.dumps(state["agent_tree"])),
            )
            conn.execute(
                "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                ("tick_engine", json.dumps(state["tick_engine"])),
            )
            conn.execute(
                "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                ("state_epoch", json.dumps(state["state_epoch"])),
            )
            conn.execute(
                "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                ("private_store_base_path", json.dumps(state["private_store_base_path"])),
            )
            for key in [
                "tasks", "emails", "scheduler", "outbox", "pending_ops",
                "kb", "locks", "audit", "file_ops_audit", "agent_states",
            ]:
                conn.execute(
                    "INSERT OR REPLACE INTO state (component, payload) VALUES (?, ?)",
                    (key, json.dumps(state[key])),
                )

        # Should load without error
        sim = Simulation.load_from(path)
        assert len(sim._journal) == 0
        assert len(sim.audit_log) == 0
