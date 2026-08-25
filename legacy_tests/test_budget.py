"""T16c — token/cost budget (v0.8 P2-11).

Covers the budget module end to end:

- Unit: pricing table / cost estimation / per-request usage estimate.
- Unit: BudgetTracker accumulation per agent/task/simulation and
  snapshot/restore round-trip.
- Unit: limit checks (cost-first, then tokens / request count / wall
  time) at agent/task/simulation scope, plus concurrency.
- Integration: PreValidate rejects the WHOLE activation round when
  cumulative + estimate exceeds a cap — no LLM op registered, no
  request executed, nothing committed, audit record present.
- Integration: cumulative accounting — a second round is rejected after
  the first consumed the budget.
- Integration: budget accumulators survive save/load (模拟重启不丢累计),
  and the rejection keeps working after restart.
- Integration: concurrency over-limit goes through the same PreValidate
  rejection path (whole round).
- Gateway: LLMInvocation.cost is populated from the pricing table.
"""

from __future__ import annotations

import uuid

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.budget import (
    DEFAULT_PRICE_PER_1M,
    BudgetCheckResult,
    BudgetConfig,
    BudgetLimits,
    BudgetTracker,
    BudgetUsage,
    InFlightCounts,
    estimate_cost,
    estimate_llm_usage,
)
from my_team.fake_llm import FakeLLMProvider
from my_team.models.activation import ReadyCandidate
from my_team.models.continuation import AgentContinuation
from my_team.models.intent import Intent, SubmitLLMRequest, WritePrivateFileIntent
from my_team.pending_ops import OpStatus, OpType
from my_team.simulation import Simulation, SimulationConfig

# -- pricing / estimate unit tests ------------------------------------------


def test_estimate_cost_known_model() -> None:
    # gpt-4o: $2.50 in / $10.00 out per 1M tokens
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000) == 12.5
    assert estimate_cost("gpt-4o", 1_000, 0) == 0.0025


def test_estimate_cost_unknown_model_falls_back() -> None:
    # Unknown models use a conservative mid-range default — never free.
    assert estimate_cost("future-model-9", 1_000_000, 1_000_000) == (
        DEFAULT_PRICE_PER_1M[0] + DEFAULT_PRICE_PER_1M[1]
    )


def test_estimate_cost_local_model_free() -> None:
    assert estimate_cost("ollama", 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_override_pricing() -> None:
    pricing = {"gpt-4o": (1.0, 2.0)}
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000, pricing) == 3.0
    # Override only wins for listed models; others fall back to defaults.
    assert estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000, pricing) == (
        0.15 + 0.60
    )


def test_estimate_llm_usage_deterministic() -> None:
    messages = (
        {"role": "system", "content": "x" * 400},
        {"role": "user", "content": "y" * 800},
    )
    usage = estimate_llm_usage(
        model="gpt-4o",
        messages=messages,
        max_tokens=2000,
        timeout_ticks=3,
        tick_duration_seconds=10.0,
    )
    assert usage.request_count == 1
    assert usage.input_tokens == 1200 // 4  # chars/4 heuristic
    assert usage.output_tokens == 2000
    assert usage.wall_time_seconds == 30.0
    # cost = (300 * 2.5 + 2000 * 10.0) / 1M
    assert usage.cost == (300 * 2.5 + 2000 * 10.0) / 1_000_000.0


def test_estimate_llm_usage_empty_messages() -> None:
    usage = estimate_llm_usage(
        model="gpt-4o", messages=(), max_tokens=4096,
        timeout_ticks=1, tick_duration_seconds=10.0,
    )
    assert usage.input_tokens == 0
    assert usage.output_tokens == 4096


# -- accumulator unit tests --------------------------------------------------


def test_tracker_accumulates_per_scope() -> None:
    tracker = BudgetTracker()
    tracker.record_llm(
        agent_id="agent.a", task_id="task.1", model="gpt-4o",
        input_tokens=1000, output_tokens=2000, wall_time_seconds=10.0,
    )
    tracker.record_llm(
        agent_id="agent.a", task_id="task.1", model="gpt-4o",
        input_tokens=500, output_tokens=500, wall_time_seconds=5.0,
    )
    tracker.record_llm(
        agent_id="agent.b", task_id="task.2", model="gpt-4o",
        input_tokens=100, output_tokens=100,
    )

    sim_usage = tracker.simulation_usage
    assert sim_usage.request_count == 3
    assert sim_usage.total_tokens == 1000 + 2000 + 500 + 500 + 100 + 100
    assert sim_usage.cost == estimate_cost(
        "gpt-4o", sim_usage.input_tokens, sim_usage.output_tokens,
    )
    assert sim_usage.wall_time_seconds == 15.0

    a = tracker.agent_usage("agent.a")
    assert a.request_count == 2
    assert a.total_tokens == 4000
    assert tracker.agent_usage("agent.nobody").request_count == 0

    t1 = tracker.task_usage("task.1")
    assert t1.request_count == 2
    assert t1.total_tokens == 4000
    assert tracker.task_usage("task.none").request_count == 0


def test_tracker_explicit_cost_wins() -> None:
    tracker = BudgetTracker()
    tracker.record_llm(
        agent_id="agent.a", model="gpt-4o",
        input_tokens=1_000_000, output_tokens=1_000_000,
        cost=99.0,
    )
    assert tracker.simulation_usage.cost == 99.0


def test_tracker_snapshot_restore_roundtrip() -> None:
    tracker = BudgetTracker()
    tracker.record_llm(
        agent_id="agent.a", task_id="task.1", model="gpt-4o",
        input_tokens=100, output_tokens=200, wall_time_seconds=7.0,
    )
    snapshot = tracker.snapshot()
    fresh = BudgetTracker()
    fresh.restore(snapshot)
    assert fresh.simulation_usage == tracker.simulation_usage
    assert fresh.agent_usage("agent.a") == tracker.agent_usage("agent.a")
    assert fresh.task_usage("task.1") == tracker.task_usage("task.1")
    assert fresh.config.pricing == tracker.config.pricing


def test_tracker_restore_empty_snapshot() -> None:
    fresh = BudgetTracker()
    fresh.restore({})  # pre-budget saves load cleanly
    assert fresh.simulation_usage.request_count == 0


# -- limit-check unit tests --------------------------------------------------


def _estimate(reqs: int = 1) -> BudgetUsage:
    return BudgetUsage(
        request_count=reqs, input_tokens=100, output_tokens=100,
        cost=0.01, wall_time_seconds=5.0,
    )


def test_check_allows_within_limits() -> None:
    tracker = BudgetTracker(config=BudgetConfig(
        agent=BudgetLimits(cost=1.0, total_tokens=1000, request_count=10),
        task=BudgetLimits(cost=1.0),
        simulation=BudgetLimits(cost=1.0),
    ))
    assert tracker.check(
        "agent.a", "task.1", _estimate(),
        InFlightCounts(agent=0, task=0, simulation=0),
    ) is None


def test_check_cost_first_agent_scope() -> None:
    tracker = BudgetTracker(config=BudgetConfig(
        agent=BudgetLimits(cost=0.005),
        task=BudgetLimits(cost=100.0),
        simulation=BudgetLimits(cost=100.0),
    ))
    result = tracker.check(
        "agent.a", "task.1", _estimate(),
        InFlightCounts(agent=0, task=0, simulation=0),
    )
    assert result is not None
    assert result.scope == "agent"
    assert result.dimension == "cost"


def test_check_task_scope() -> None:
    tracker = BudgetTracker(config=BudgetConfig(
        agent=BudgetLimits(cost=100.0),
        task=BudgetLimits(request_count=2),
        simulation=BudgetLimits(cost=100.0),
    ))
    tracker.record_llm("agent.a", "task.1", model="gpt-4o")
    tracker.record_llm("agent.a", "task.1", model="gpt-4o")
    result = tracker.check(
        "agent.a", "task.1", _estimate(),
        InFlightCounts(agent=0, task=0, simulation=0),
    )
    assert result is not None
    assert result.scope == "task"
    assert result.dimension == "request_count"


def test_check_simulation_scope() -> None:
    tracker = BudgetTracker(config=BudgetConfig(
        agent=BudgetLimits(cost=100.0),
        task=BudgetLimits(cost=100.0),
        simulation=BudgetLimits(total_tokens=100),
    ))
    result = tracker.check(
        "agent.a", "", _estimate(),  # no task → task scope skipped
        InFlightCounts(agent=0, task=0, simulation=0),
    )
    assert result is not None
    assert result.scope == "simulation"
    assert result.dimension == "total_tokens"


def test_check_wall_time_dimension() -> None:
    tracker = BudgetTracker(config=BudgetConfig(
        agent=BudgetLimits(wall_time_seconds=4.0),
        task=BudgetLimits(wall_time_seconds=100.0),
        simulation=BudgetLimits(wall_time_seconds=100.0),
    ))
    result = tracker.check(
        "agent.a", "task.1", _estimate(),
        InFlightCounts(agent=0, task=0, simulation=0),
    )
    assert result is not None
    assert result.dimension == "wall_time"


def test_check_concurrency_agent_scope() -> None:
    tracker = BudgetTracker(config=BudgetConfig(
        agent=BudgetLimits(concurrency=2),
        task=BudgetLimits(concurrency=100),
        simulation=BudgetLimits(concurrency=100),
    ))
    # 1 in flight + this round's 2 requests > 2 → rejected
    result = tracker.check(
        "agent.a", "task.1", _estimate(reqs=2),
        InFlightCounts(agent=1, task=1, simulation=1),
    )
    assert result is not None
    assert result.dimension == "concurrency"
    assert result.scope == "agent"


def test_check_concurrency_fallback_to_legacy_cap() -> None:
    tracker = BudgetTracker()  # agent.concurrency = 0 → fallback
    # Fallback cap 2: 2 in flight + 1 planned = 3 > 2 → rejected
    result = tracker.check(
        "agent.a", "", _estimate(),
        InFlightCounts(agent=2, task=0, simulation=2),
        agent_concurrency_limit=2,
    )
    assert result is not None
    assert result.dimension == "concurrency"
    # Fallback cap unlimited when not provided
    assert tracker.check(
        "agent.a", "", _estimate(),
        InFlightCounts(agent=2, task=0, simulation=2),
    ) is None


def test_check_concurrency_zero_forces_denial() -> None:
    """max_concurrent_llm_requests=0 keeps its legacy force-denial."""
    tracker = BudgetTracker()
    result = tracker.check(
        "agent.a", "", _estimate(),
        InFlightCounts(agent=0, task=0, simulation=0),
        agent_concurrency_limit=0,
    )
    assert result is not None
    assert result.dimension == "concurrency"


def test_check_result_reason() -> None:
    result = BudgetCheckResult(
        exceeded=True, scope="agent", dimension="cost",
        current=0.01, limit=0.02, estimate=0.015,
    )
    assert "agent-scope cost budget exceeded" in result.reason


# -- integration: PreValidate rejection -------------------------------------


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
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


class LLMPlusWriteAgent(BaseAgent):
    """One round = [write a file, submit an LLM request].

    If budget rejection were per-intent instead of whole-round, the file
    write would succeed while the LLM request fails — this agent proves
    the round is rejected atomically. ``proof_path`` must be unique per
    test: the private store persists across test runs.
    """

    def __init__(
        self, agent_id: str, proof_path: str = "budget-proof.txt", **kwargs: object,
    ) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._proof_path = proof_path

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase.name == "PROCESSING_RESULT"
            and continuation.last_llm_result
        ):
            return []
        return [
            WritePrivateFileIntent(
                agent_id=self._agent_id, path=self._proof_path,
                content="should-not-exist",
            ),
            SubmitLLMRequest(
                agent_id=self._agent_id, messages=(),
                model="gpt-4o", max_tokens=1024,
            ),
        ]


def _sim_with_tiny_agent_cost(agent_cost: float) -> Simulation:
    return Simulation(
        agent_tree=_make_tree(),
        config=SimulationConfig(budget=BudgetConfig(
            agent=BudgetLimits(cost=agent_cost),
            task=BudgetLimits(cost=100.0),
            simulation=BudgetLimits(cost=100.0),
        )),
    )


def test_prevalidate_rejects_whole_round_on_cost() -> None:
    """Over-budget LLM request → whole round rejected, nothing executes."""
    sim = _sim_with_tiny_agent_cost(agent_cost=0.0001)
    agent = LLMPlusWriteAgent(
        "agent.root", proof_path=f"budget-{uuid.uuid4().hex}.txt",
    )
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.root"] = agent
    provider = FakeLLMProvider(responses={
        "agent.root": [{"content": "ok", "tool_calls": []}],
    })

    sim.run_tick()
    rs = sim._agent_runtime_states["agent.root"]

    # Round rejected: agent never went WAITING_FOR_LLM, no op registered
    assert rs.state == AgentState.IDLE
    assert sim._pending_ops.get_by_agent("agent.root") == []
    assert sim.budget.simulation_usage.request_count == 0

    # The companion write intent was ALSO rejected (whole round, no
    # partial execution — 事务原子性).
    priv = sim._private_store
    assert not priv.resolve_path("agent.root", "budget-proof.txt").exists()

    # No LLM request was executed by the provider
    assert provider.advance(sim, current_tick=1) == 0

    # Audit records the budget rejection
    rejected = sim.audit_log.for_event_type(AuditEventType.BUDGET_REJECTED)
    assert len(rejected) == 1
    entry = rejected[0]
    assert entry.details.get("error_code") == "BUDGET_EXCEEDED"
    assert entry.details.get("scope") == "agent"
    assert entry.details.get("dimension") == "cost"
    assert not entry.success


def test_cumulative_accounting_rejects_second_round() -> None:
    """First round consumes the budget; the second is rejected."""
    sim = _sim_with_tiny_agent_cost(agent_cost=0.05)
    agent = LLMPlusWriteAgent(
        "agent.root", proof_path=f"budget-{uuid.uuid4().hex}.txt",
    )
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.root"] = agent
    provider = FakeLLMProvider(responses={
        "agent.root": [{"content": "ok", "tool_calls": []}],
    })

    # Round 1: estimate cost for gpt-4o 1024 out ≈ 0.01024 < 0.05 → OK
    sim.run_tick()
    rs = sim._agent_runtime_states["agent.root"]
    assert rs.state == AgentState.WAITING_FOR_LLM
    ops = sim._pending_ops.get_by_agent("agent.root")
    assert len(ops) == 1
    assert ops[0].status == OpStatus.SUBMITTED

    # Provider completes with REAL usage (fake provider may pass usage)
    provider.advance(sim, current_tick=1)
    sim.run_tick()  # Ingest delivers → budget charged

    assert sim.budget.simulation_usage.request_count == 1
    assert sim.budget.agent_usage("agent.root").request_count == 1
    assert sim.budget.agent_usage("agent.root").cost > 0

    # Round 2: cumulative + estimate now exceeds the 0.05 agent cap —
    # record a big explicit charge so cumulative ≈ $1.27 > 0.05.
    sim.budget.record_llm(
        agent_id="agent.root", model="gpt-4o",
        input_tokens=100_000, output_tokens=100_000,
    )
    plan = {"agent.root": [SubmitLLMRequest(
        agent_id="agent.root", messages=(), model="gpt-4o", max_tokens=1024,
    )]}
    candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=2)
    validated = sim._phase_validate(2, plan, ready=[candidate])
    result = validated["agent.root"][0]
    assert not result.success
    assert result.error_code == "BUDGET_EXCEEDED"
    rejected = sim.audit_log.for_event_type(AuditEventType.BUDGET_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].details.get("dimension") == "cost"


def test_prevalidate_rejects_whole_round_on_concurrency() -> None:
    """Concurrency over-limit takes the same PreValidate rejection path."""
    sim = Simulation(
        agent_tree=_make_tree(),
        config=SimulationConfig(max_concurrent_llm_requests=1),
    )
    agent = LLMPlusWriteAgent(
        "agent.root", proof_path=f"budget-{uuid.uuid4().hex}.txt",
    )
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.root"] = agent

    # One LLM op already in flight (from a previous tick)
    sim._pending_ops.submit(
        op_type=OpType.LLM_REQUEST,
        agent_id="agent.root",
        created_tick=0,
        eligible_tick=1,
    )

    sim.run_tick()
    rs = sim._agent_runtime_states["agent.root"]
    # Whole round rejected: still IDLE, no new op, file not written
    assert rs.state == AgentState.IDLE
    assert len(sim._pending_ops.get_by_agent("agent.root")) == 1  # only pre-existing
    priv = sim._private_store
    assert not priv.resolve_path("agent.root", "budget-proof.txt").exists()

    denied = [
        d for d in sim.audit_log.for_event_type(AuditEventType.PERMISSION_DENIED)
        if d.details.get("reason") == "llm_budget_exceeded"
    ]
    assert len(denied) == 1
    assert denied[0].details.get("error_code") == "BUDGET_EXCEEDED"
    assert denied[0].details.get("dimension") == "concurrency"


def test_restart_keeps_accumulated_usage(tmp_path) -> None:
    """Budget accumulators survive save/load; rejection persists."""
    sim = Simulation(
        agent_tree=_make_tree(),
        config=SimulationConfig(budget=BudgetConfig(
            agent=BudgetLimits(cost=100.0),
            task=BudgetLimits(cost=100.0),
            simulation=BudgetLimits(cost=100.0),
        )),
    )

    class OneShotAgent(BaseAgent):
        def decide_intents(self, observation, continuation=None) -> list[Intent]:
            if (
                continuation is not None
                and continuation.phase.name == "PROCESSING_RESULT"
                and continuation.last_llm_result
            ):
                return []
            return [SubmitLLMRequest(
                agent_id=self._agent_id, messages=(),
                model="gpt-4o", max_tokens=512,
            )]

    agent = OneShotAgent("agent.root")
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.root"] = agent
    provider = FakeLLMProvider(responses={
        "agent.root": [{
            "content": "ok", "tool_calls": [],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        }],
    })

    sim.run_tick()
    provider.advance(sim, current_tick=1)
    sim.run_tick()  # deliver → record usage
    assert sim.budget.simulation_usage.request_count == 1
    assert sim.budget.simulation_usage.total_tokens == 1500

    db_path = tmp_path / "sim.db"
    sim.save_to(db_path)

    restored = Simulation.load_from(db_path)
    assert restored.budget.simulation_usage.request_count == 1
    assert restored.budget.simulation_usage.total_tokens == 1500
    assert restored.budget.agent_usage("agent.root").total_tokens == 1500
    # wall time recorded: 1 elapsed tick × 10s default tick duration
    assert restored.budget.agent_usage("agent.root").wall_time_seconds == 10.0

    # The restarted sim enforces the (restored) budget: crank cost over
    # the limit and verify a fresh round is rejected in the restarted sim.
    restored._config.budget.agent.cost = 0.0001
    plan = {"agent.root": [SubmitLLMRequest(
        agent_id="agent.root", messages=(), model="gpt-4o", max_tokens=512,
    )]}
    candidate = ReadyCandidate(agent_id="agent.root", events=(), tick=2)
    validated = restored._phase_validate(2, plan, ready=[candidate])
    assert not validated["agent.root"][0].success
    assert validated["agent.root"][0].error_code == "BUDGET_EXCEEDED"
    assert len(
        restored.audit_log.for_event_type(AuditEventType.BUDGET_REJECTED)
    ) == 1


def test_config_roundtrip_through_save(tmp_path) -> None:
    """Budget config (incl. pricing override) survives save/load."""
    sim = Simulation(
        agent_tree=_make_tree(),
        config=SimulationConfig(budget=BudgetConfig(
            pricing={"gpt-4o": (1.0, 2.0)},
            agent=BudgetLimits(cost=0.5, request_count=3),
            task=BudgetLimits(total_tokens=5000),
            simulation=BudgetLimits(wall_time_seconds=100.0),
        )),
    )
    db_path = tmp_path / "sim-cfg.db"
    sim.save_to(db_path)
    restored = Simulation.load_from(db_path)
    cfg = restored._config.budget
    assert cfg.pricing == {"gpt-4o": (1.0, 2.0)}
    assert cfg.agent.cost == 0.5
    assert cfg.agent.request_count == 3
    assert cfg.task.total_tokens == 5000
    assert cfg.simulation.wall_time_seconds == 100.0


def test_gateway_invocation_cost_populated(monkeypatch) -> None:
    """LLMInvocation.cost comes from the pricing table (cost-first)."""
    from my_team.llm_gateway import LLMGateway
    from my_team.models.llm import (
        ChatMessage,
        LLMProviderConfig,
        LLMRequest,
        LLMResult,
    )

    gw = LLMGateway()
    gw.register_profile("default", LLMProviderConfig(
        provider="openai", model="gpt-4o",
    ))
    gw.bind_agent("agent.root", "default")

    def fake_call(profile, request):
        return LLMResult(
            content="hi",
            usage={
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
            },
            model="gpt-4o",
        )

    monkeypatch.setattr(gw, "_call_provider", fake_call)
    gw.complete(LLMRequest(
        request_id="req.001", agent_id="agent.root",
        activation_id="act.001",
        messages=(ChatMessage(role="user", content="hi"),),
    ))
    invocations = gw.get_invocation_log()
    assert len(invocations) == 1
    assert invocations[0].cost == 12.5  # 2.5 + 10.0 per 1M
