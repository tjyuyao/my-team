"""Persistence tests (P3-11: SQLite save/load + crash recovery).

Verifies:
- SimulationStore atomic save/load (one transaction, all-or-nothing)
- Full state roundtrip: tasks, emails, outbox, pending ops, KB
  (resources + versions + permissions), locks, audit, scheduler events,
  agent state machines + continuations, state epoch
- Deterministic continuation: a loaded simulation behaves identically
- Crash recovery: pause → save → shutdown → load → resume, with
  quarantined external results delivered after resume
- Corruption/schema mismatch → clean failure
"""

from __future__ import annotations

import sqlite3

import pytest

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.fake_llm import FakeLLMProvider
from my_team.models.activation import WakeEventType
from my_team.models.continuation import ContinuationPhase
from my_team.models.intent import Intent, SubmitLLMRequest
from my_team.persistence import SCHEMA_VERSION, SimulationStore
from my_team.shared_kb import PermissionRule
from my_team.simulation import Simulation


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class SubmitAgent(BaseAgent):
    """Agent that submits one async LLM request and waits."""

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation=None,
    ) -> list[Intent]:
        return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]


def _install_agents(sim: Simulation, *agent_ids: str) -> None:
    for aid in agent_ids:
        agent = SubmitAgent(aid)
        agent._tool_registry = sim._tool_registry
        sim._runtimes[aid] = agent


class TestSimulationStore:
    """SQLite store unit tests."""

    def test_save_load_roundtrip(self, tmp_path) -> None:
        store = SimulationStore(tmp_path / "sim.db")
        store.save({"a": {"x": 1}, "b": [1, 2, 3]})
        state = store.load()
        assert state == {"a": {"x": 1}, "b": [1, 2, 3]}
        assert store.schema_version() == SCHEMA_VERSION

    def test_load_missing_returns_none(self, tmp_path) -> None:
        store = SimulationStore(tmp_path / "nope.db")
        assert store.load() is None
        assert store.schema_version() is None

    def test_wipe(self, tmp_path) -> None:
        store = SimulationStore(tmp_path / "sim.db")
        store.save({"a": 1})
        assert store.load() is not None
        store.wipe()
        assert store.load() is None


class TestSaveLoadRoundtrip:
    """Full simulation state survives save → load."""

    def test_rich_state_survives(self, tmp_path) -> None:
        """Tasks, emails, outbox, pending op, KB, locks, audit, scheduler,
        continuation all survive a roundtrip."""
        sim = Simulation(agent_tree=_make_tree())
        _install_agents(sim, "agent.root", "agent.research")

        # Run one tick: root submits an async LLM request (pending op)
        sim.run_tick()
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.state == AgentState.WAITING_FOR_LLM
        op = sim._pending_ops.get_by_agent("agent.root")[0]

        # Direct state: task, KB resource + permission + lock, outbox entry
        sim.task_tree.create(
            task_id="task.001", title="T1",
            creator_agent_id="agent.root", owner_agent_id="agent.research",
        )
        sim._permission_engine.add_rules([
            PermissionRule(
                scope="project/*",
                principal="agent.root",
                allow=["read", "create", "write", "kb_write", "lock", "unlock"],
            ),
        ])
        lock = sim._lock_manager.acquire(
            "project/notes.md", "agent.root", current_tick=0,
        )
        sim._shared_kb.create(
            path="project/notes.md", agent_id="agent.root",
            content="v1", tick=0,
        )
        outbox_entry = sim._outbox.stage(
            from_agent="agent.root", to=["agent.research"],
            subject="Pending mail", idempotency_key="stable-1",
        )
        sim._outbox.commit(outbox_entry.entry_id)
        # A queued wake event for the next tick
        from my_team.models.activation import WakeupEvent
        sim._scheduler.enqueue_event(WakeupEvent(
            event_type=WakeEventType.NEW_EMAIL,
            target_agent_id="agent.research",
            tick=0,
            source_agent_id="agent.root",
        ))

        db = tmp_path / "sim.db"
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        # Runtime LOGIC is not persisted — re-install the custom agents
        _install_agents(sim2, "agent.root", "agent.research")
        assert sim2.current_tick == sim.current_tick == 1

        # Tasks
        assert sim2.task_tree.exists("task.001")
        t = sim2.task_tree.get("task.001")
        assert t.title == "T1" and t.owner_agent_id == "agent.research"

        # Outbox: committed entry survives and can still dispatch
        entry = sim2._outbox.get(outbox_entry.entry_id)
        assert entry is not None
        assert entry.status.value == "committed"

        # Pending op + continuation: agent still waiting on the SAME request
        rs2 = sim2._agent_runtime_states["agent.root"]
        assert rs2.state == AgentState.WAITING_FOR_LLM
        assert rs2.continuation.phase == ContinuationPhase.WAITING_FOR_LLM
        assert rs2.continuation.pending_request_id == op.request_id
        ops2 = sim2._pending_ops.get_by_agent("agent.root")
        assert len(ops2) == 1
        assert ops2[0].request_id == op.request_id
        assert ops2[0].state_epoch == sim2.state_epoch

        # KB: content + version + permission rules + lock
        resource = sim2._shared_kb.read("project/notes.md", "agent.root")
        assert resource.content == "v1" and resource.version == 1
        assert sim2._shared_kb.versions.get_version("project/notes.md") == 1
        assert sim2._permission_engine.check(
            "agent.root", "project/x.md", "kb_write",
        )
        assert sim2._lock_manager.get_lock("project/notes.md") is not None
        assert sim2._lock_manager.get_lock("project/notes.md").lock_token \
            == lock.lock_token

        # Scheduler: queued event survives
        events = sim2._scheduler.all_events()
        assert any(
            qe.event.event_type == WakeEventType.NEW_EMAIL
            and qe.event.target_agent_id == "agent.research"
            for qe in events
        )

        # Audit: same entry count
        assert len(sim2.audit_log) == len(sim.audit_log)

    def test_state_epoch_survives_and_fencing_works(self, tmp_path) -> None:
        """Epoch bumps survive; stale results are still fenced after load."""
        sim = Simulation(agent_tree=_make_tree())
        _install_agents(sim, "agent.root")
        sim.run_tick()
        op = sim._pending_ops.get_by_agent("agent.root")[0]

        sim._bump_state_epoch()
        db = tmp_path / "sim.db"
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        assert sim2.state_epoch == 1
        assert sim2._pending_ops.get_by_id(op.request_id).state_epoch == 0

        # Late result for the old-epoch op is discarded after load
        sim2._pending_ops.complete(op.request_id, result={"content": "late"})
        sim2.run_tick()
        rs2 = sim2._agent_runtime_states["agent.root"]
        assert rs2.continuation.last_llm_result == {}
        assert sim2._pending_ops.get_by_agent("agent.root") == []

    def test_deterministic_lockstep(self, tmp_path) -> None:
        """A loaded simulation behaves identically to the original.

        Both sims run the same scripted LLM self-email loop after load;
        observable state (emails, audit event sequence, continuations,
        activations) must match exactly.
        """
        import json as _json

        from my_team.agent_runtime import action_plan_to_intents
        from my_team.prompt_templates import PromptTemplates

        class LoopAgent(BaseAgent):
            """Async LLM agent: on a response, sends an email to itself
            (which re-activates it next tick → submits again)."""

            def __init__(self, agent_id: str, **kwargs: object) -> None:
                super().__init__(agent_id=agent_id, **kwargs)
                self._templates = PromptTemplates()

            def decide_intents(self, observation, continuation=None) -> list[Intent]:
                if (
                    continuation is not None
                    and continuation.phase == ContinuationPhase.PROCESSING_RESULT
                    and continuation.last_llm_result
                ):
                    result = continuation.last_llm_result
                    plan = self._templates.parse_llm_response(
                        content=result.get("content", ""),
                        tool_calls=list(result.get("tool_calls", [])),
                        agent_id=self._agent_id,
                        tick=observation.tick,
                    )
                    return action_plan_to_intents(plan)
                return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]

        def _script(n: int, start: int = 1) -> list[dict]:
            return [
                {
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "send_email",
                            "arguments": _json.dumps({
                                "to": ["agent.root"],
                                "subject": f"loop {i}",
                                "body": "ping",
                            }),
                        },
                    }],
                }
                for i in range(start, start + n)
            ]

        sim = Simulation(agent_tree=_make_tree())
        provider = FakeLLMProvider(latency_ticks=1, responses={
            "agent.root": _script(6),
        })
        root = LoopAgent("agent.root")
        root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = root

        # Phase 1 (original sim): submit → response → email → re-activate
        for tick in range(1, 4):
            provider.advance(sim, current_tick=tick)
            sim.run_tick()

        db = tmp_path / "sim.db"
        sim.save_to(db)

        # "Restart": load and re-install runtime logic. The fake provider's
        # script index is EXTERNAL harness state (not persisted) — sim2's
        # provider must resume where sim1's left off, so we read sim1's
        # consumed-response counter and continue the script from there.
        sim2 = Simulation.load_from(db)
        root2 = LoopAgent("agent.root")
        root2._tool_registry = sim2._tool_registry
        sim2._runtimes["agent.root"] = root2
        used = provider._call_counters.get("agent.root", 0)
        provider2 = FakeLLMProvider(latency_ticks=1, responses={
            "agent.root": _script(6, start=used + 1),
        })

        # Phase 2: run both in lockstep for 3 more ticks
        for tick in range(4, 7):
            provider.advance(sim, current_tick=tick)
            provider2.advance(sim2, current_tick=tick)
            sim.run_tick()
            sim2.run_tick()

        def fingerprint(s: Simulation) -> dict:
            rs = s._agent_runtime_states["agent.root"]
            return {
                "tick": s.current_tick,
                "epoch": s.state_epoch,
                "audit_events": [
                    e.event_type.value for e in s.audit_log.entries
                ],
                "agent_state": rs.state.value,
                "cont_phase": rs.continuation.phase.value,
                "react_turn": rs.continuation.react_turn,
                "llm_calls": rs.continuation.total_llm_calls,
                "activations": len(s.scheduler.get_activation_history()),
                "emails": sorted(
                    e.subject for e in s._mail_system._all_emails.values()
                ),
            }

        assert fingerprint(sim) == fingerprint(sim2)


class TestCrashRecovery:
    """Pause → save → shutdown → load → resume."""

    def test_pause_save_load_resume_with_quarantine(self, tmp_path) -> None:
        """External results completed while paused are quarantined in the
        saved state and delivered after load + resume."""
        sim = Simulation(agent_tree=_make_tree())
        _install_agents(sim, "agent.root")

        sim.run_tick()  # root submits LLM request
        sim.pause()
        op = sim._pending_ops.get_by_agent("agent.root")[0]

        # While paused the provider completes the op → quarantined
        sim._pending_ops.complete(op.request_id, result={"content": "hello"})
        db = tmp_path / "sim.db"
        sim.save_to(db)

        # "Crash": reconstruct from disk (runtime logic re-installed)
        sim2 = Simulation.load_from(db)
        _install_agents(sim2, "agent.root")
        assert sim2.is_paused
        assert sim2._pending_ops.get_by_id(op.request_id).status.value \
            == "completed"

        # Resume → the quarantined result is ingested and delivered
        sim2.resume()
        sim2.run_tick()
        rs2 = sim2._agent_runtime_states["agent.root"]
        assert rs2.continuation.react_turn == 1
        assert "llm_result_received" in [
            e["event"] for e in rs2.continuation.event_log
        ]
        # The agent was re-activated and submitted a new request
        ops2 = sim2._pending_ops.get_by_agent("agent.root")
        assert len(ops2) == 1
        assert ops2[0].request_id != op.request_id


class TestLoadErrors:
    """Corrupt or mismatched databases fail cleanly."""

    def test_load_missing_db_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            Simulation.load_from(tmp_path / "absent.db")

    def test_corrupt_payload_raises(self, tmp_path) -> None:
        sim = Simulation(agent_tree=_make_tree())
        db = tmp_path / "sim.db"
        sim.save_to(db)

        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE state SET payload = ? WHERE component = 'tasks'",
                ("{not json",),
            )
        with pytest.raises(ValueError):
            Simulation.load_from(db)

    def test_schema_mismatch_raises(self, tmp_path) -> None:
        sim = Simulation(agent_tree=_make_tree())
        db = tmp_path / "sim.db"
        sim.save_to(db)

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", "999"),
            )
        with pytest.raises(ValueError, match="schema version"):
            Simulation.load_from(db)

    def test_save_does_not_corrupt_previous_state(self, tmp_path) -> None:
        """A save always leaves a loadable database (all-or-nothing)."""
        sim = Simulation(agent_tree=_make_tree())
        db = tmp_path / "sim.db"
        sim.run_tick()
        sim.save_to(db)
        sim2 = Simulation.load_from(db)
        assert sim2.current_tick == 1

        # Saving again (after more ticks) replaces cleanly
        sim.run_tick()
        sim.save_to(db)
        sim3 = Simulation.load_from(db)
        assert sim3.current_tick == 2
