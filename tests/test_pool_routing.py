"""Tests for T11 决策 3: WorkerPool = service manager + children + rules.

Covers:
- PoolConfig validation (requires kind=service)
- Immediate mode: same-tick expansion into original + copy
  (derived_from) + worker notice, one atomic group
- Strategies: least_busy / round_robin / skill_match (with fallback)
- Deferred mode: stateless pending derivation; dispatch when a child
  is idle; single-point serial assignment (no claim races)
- Bare service agent (no pool config) rejects delegation
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from my_team.agent_tree import AgentTree
from my_team.models.activation import ReadyCandidate
from my_team.models.agent import AgentConfig, PoolConfig, PoolMode, PoolStrategy
from my_team.models.intent import DelegateIntent
from my_team.models.task import TaskStatus

_BASE = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
_WORKERS = ("agent.w1", "agent.w2", "agent.w3")


def _tree(pool: PoolConfig | None, skills: dict[str, list[str]] | None):
    """Root → pool-manager(service) → three workers."""
    manager: dict = {
        "agent_id": "agent.pool",
        "display_name": "Pool",
        "role": "pool_manager",
        "kind": "service",
        "parent_id": "agent.root",
        "children": list(_WORKERS),
        "tools": [],
        "can_delegate": True,
        "metadata": {"bootstrap": False},
    }
    if pool is not None:
        manager["pool"] = {
            "mode": pool.mode.value,
            "strategy": pool.strategy.value,
        }
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.pool"],
                "tools": ["read", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": False},
            },
            manager,
            *[
                {
                    "agent_id": wid,
                    "display_name": wid,
                    "role": "worker",
                    "parent_id": "agent.pool",
                    "children": [],
                    "tools": ["read"],
                    "can_delegate": False,
                    "metadata": {
                        "skills": (skills or {}).get(wid, []),
                    },
                }
                for wid in _WORKERS
            ],
        ],
    })


def _make_sim(pool: PoolConfig | None = PoolConfig(),
              skills: dict[str, list[str]] | None = None):
    from my_team.simulation import Simulation

    sim = Simulation(agent_tree=_tree(pool, skills))
    engine = sim.tick_engine
    engine._config.tick_duration_value = 10
    engine._config.tick_duration_unit = "minutes"
    engine._anchor = _BASE
    return sim


def _delegate(sim, recipient="agent.pool", skill=None, title="T"):
    """Run a real DelegateIntent through Validate → Act → Commit."""
    intent = DelegateIntent(
        agent_id="agent.root",
        recipient_agent_id=recipient,
        task_title=title,
        task_description="do it",
        deadline=_BASE + timedelta(hours=2),
        skill=skill,
    )
    tick = sim.tick_engine.current_tick
    plan: dict = {"agent.root": [intent]}
    candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=tick)
    validated = sim._phase_validate(tick, plan, ready=[candidate])
    act_results = sim._phase_act(
        tick, plan, ready=[candidate], validated=validated,
    )
    sim._phase_commit(tick, act_results)
    return act_results


def _copies(sim):
    return [t for t in sim.task_tree if t.derived_from is not None]


class TestPoolConfigValidation:
    def test_pool_requires_service_kind(self):
        with pytest.raises(ValueError, match="kind='service'"):
            AgentConfig(
                agent_id="a", display_name="A", role="r",
                kind="llm", pool=PoolConfig(),
            )

    def test_service_without_pool_is_legal(self):
        cfg = AgentConfig(agent_id="a", display_name="A", role="r",
                          kind="service")
        assert cfg.pool is None


class TestImmediateMode:
    def test_delegation_expands_to_original_plus_copy(self):
        sim = _make_sim()
        _delegate(sim)
        originals = [
            tid for tid in sim.task_tree.all_ids()
            if sim.task_tree.get(tid).assignee_agent_id == "agent.pool"
        ]
        assert len(originals) == 1
        copies = _copies(sim)
        assert len(copies) == 1
        copy = copies[0]
        assert copy.assignee_agent_id in _WORKERS
        assert copy.derived_from == originals[0]
        assert copy.parent_task_id == originals[0]
        assert copy.deadline == _BASE + timedelta(hours=2)

    def test_least_busy_picks_idle_child(self):
        sim = _make_sim()
        for wid in ("agent.w1", "agent.w2"):
            sim.task_tree.create(
                task_id=f"pre.{wid}", title="load",
                assigner_agent_id="agent.root", assignee_agent_id=wid,
                status=TaskStatus.IN_PROGRESS,
            )
        _delegate(sim)
        assert _copies(sim)[0].assignee_agent_id == "agent.w3"

    def test_round_robin_cycles(self):
        sim = _make_sim(PoolConfig(strategy=PoolStrategy.ROUND_ROBIN))
        for i in range(3):
            _delegate(sim, title=f"T{i}")
        owners = [c.assignee_agent_id for c in sorted(_copies(sim),
                                                   key=lambda c: c.title)]
        assert owners == ["agent.w1", "agent.w2", "agent.w3"]

    def test_skill_match_respects_tag_with_fallback(self):
        sim = _make_sim(
            PoolConfig(strategy=PoolStrategy.SKILL_MATCH),
            skills={"agent.w2": ["refund"]},
        )
        _delegate(sim, skill="refund")
        assert _copies(sim)[0].assignee_agent_id == "agent.w2"
        # Unmatched skill falls back to least_busy (all idle → w1).
        _delegate(sim, skill="unknown-skill", title="T2")
        copies = sorted(_copies(sim), key=lambda c: c.title)
        assert len(copies) == 2
        assert copies[1].assignee_agent_id == "agent.w1"


class TestDeferredMode:
    def test_task_dispatched_when_child_idle(self):
        sim = _make_sim(PoolConfig(mode=PoolMode.DEFERRED))
        _delegate(sim)
        # Umbrella sits with the manager; no copy until an ingest runs.
        owned = [
            tid for tid in sim.task_tree.all_ids()
            if sim.task_tree.get(tid).assignee_agent_id == "agent.pool"
        ]
        assert len(owned) == 1
        assert _copies(sim) == []

        sim.run_tick()  # ingest pairs pending with idle child
        copies = _copies(sim)
        assert len(copies) == 1
        assert copies[0].assignee_agent_id in _WORKERS
        assert copies[0].derived_from == owned[0]

    def test_second_task_queues_until_worker_frees(self):
        sim = _make_sim(PoolConfig(mode=PoolMode.DEFERRED))
        _delegate(sim, title="T1")
        sim.run_tick()          # T1 → first idle worker (w1)
        _delegate(sim, title="T2")
        sim.run_tick()          # T2 → next idle worker (w2)
        assert {c.assignee_agent_id for c in _copies(sim)} == {
            "agent.w1", "agent.w2",
        }

        # Occupy every worker; queue a third umbrella task.
        for wid in _WORKERS:
            sim.task_tree.create(
                task_id=f"load.{wid}", title="load",
                assigner_agent_id="agent.root", assignee_agent_id=wid,
                status=TaskStatus.IN_PROGRESS,
            )
        _delegate(sim, title="T4")
        sim.run_tick()          # no idle child → stays queued
        assert [t for t in _copies(sim) if t.title == "T4"] == []
        queued = [
            t for t in sim.task_tree
            if t.assignee_agent_id == "agent.pool" and t.title == "T4"
        ]
        assert len(queued) == 1

        # w3 holds only its load (copies went to w1/w2) — freeing it
        # lets the next ingest dispatch T4's copy.
        sim.task_tree.update_status(
            "load.agent.w3", TaskStatus.COMPLETED,
            tick=sim.tick_engine.current_tick, allow_walk=True,
        )
        sim.run_tick()
        t4_copies = [t for t in _copies(sim) if t.title == "T4"]
        assert len(t4_copies) == 1
        assert t4_copies[0].assignee_agent_id == "agent.w3"


class TestBareServiceRejection:
    def test_delegation_to_service_without_pool_fails(self):
        sim = _make_sim(pool=None)
        intent = DelegateIntent(
            agent_id="agent.root",
            recipient_agent_id="agent.pool",
            task_title="T",
        )
        plan: dict = {"agent.root": [intent]}
        candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=0)
        validated = sim._phase_validate(0, plan, ready=[candidate])
        result = validated["agent.root"][0]
        assert not result.success
        assert "without WorkerPool config" in (result.error or "")
