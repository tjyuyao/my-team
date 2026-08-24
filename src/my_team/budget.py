"""LLM usage budget: pricing table, accumulators, and limit checks.

Per SPEC §14 (抗超负荷能力): budget-class over-limit is a NON-retryable
PreValidate rejection (可解释，不改状态), unlike capacity-class
backpressure. This module provides:

- ``DEFAULT_PRICING`` — model → (input $/1M tokens, output $/1M tokens).
  Reference prices for common models; unknown models fall back to a
  mid-range default (the budget is configurable via ``BudgetConfig``).
- ``BudgetUsage`` — one accumulator row: request_count / input_tokens /
  output_tokens / cost / wall_time_seconds (the five columns required
  by the v0.8 P2-11 card).
- ``BudgetLimits`` — per-scope caps on the five dimensions (0 = no cap).
- ``BudgetConfig`` — per-agent / per-task / per-simulation limits plus
  an optional pricing override. Exposed as ``SimulationConfig.budget``.
- ``BudgetTracker`` — cumulative accounting per agent / task /
  simulation, plus ``check()`` used by PreValidate: "累计 + 本次请求
  估算" vs the caps, returning the first exceeded limit.
- ``estimate_llm_usage()`` — deterministic per-request estimate used
  both for PreValidate ("would this request exceed the budget?") and
  as the recorded amount when a provider reports no token usage.

Accounting semantics (agreed with the main agent for T16c):
- Budget is charged ONLY for completed (delivered) LLM invocations:
  timed-out / failed / cancelled requests never reach the provider's
  usage record and are not counted.
- The rejection unit is the whole activation round: if any LLM request
  in an agent's plan would exceed a cap (cumulative + this request's
  estimate), the ENTIRE plan fails validation (no partial execution,
  consistent with tick transactional atomicity).
- Judgment is cost-first, tokens second (per the card: "预算判定以
  cost 为主、token 为辅").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------

# Reference prices in USD per 1M tokens, keyed by model name. Sourced from
# the vendors' published pricing as of 2026 (rounded); these are DEFAULTS
# only — ``BudgetConfig.pricing`` overrides per deployment. Local/free
# providers (ollama) are priced at 0 so local runs are never budget-capped
# by cost alone.
DEFAULT_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    # Anthropic
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-haiku": (0.25, 1.25),
    # Google
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # Local / free
    "ollama": (0.0, 0.0),
}

# Fallback for models not in the table: a conservative mid-range price so
# an unknown model is never silently free.
DEFAULT_PRICE_PER_1M: tuple[float, float] = (1.00, 2.00)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Estimate USD cost for a (model, token usage) triple.

    ``pricing`` overrides the default table (merged: explicit entries
    win, missing entries fall back to the defaults).
    """
    table = dict(DEFAULT_PRICING_PER_1M)
    if pricing:
        table.update(pricing)
    in_price, out_price = table.get(model, DEFAULT_PRICE_PER_1M)
    return (max(0, input_tokens) * in_price + max(0, output_tokens) * out_price) / 1_000_000.0


def estimate_input_tokens(messages: Any) -> int:
    """Rough input-token estimate for a message list.

    Same heuristic as ContextCompiler (``~4 chars per token``). Accepts
    dicts (``{"role": ..., "content": ...}``) or objects with a
    ``content`` attribute; non-dict entries contribute 0.
    """
    total_chars = 0
    for m in messages or ():
        if isinstance(m, dict):
            content = m.get("content", "")
        else:
            content = getattr(m, "content", "") or ""
        total_chars += len(str(content))
    return total_chars // 4


# ---------------------------------------------------------------------------
# Accumulator / limits / config models
# ---------------------------------------------------------------------------


class BudgetUsage(BaseModel):
    """Cumulative LLM usage for one scope (agent / task / simulation).

    Five columns per the v0.8 P2-11 card: request_count / token /
    cost / wall_time, plus input/output split for cost computation.
    """

    request_count: int = Field(default=0, description="Completed LLM requests")
    input_tokens: int = Field(default=0, description="Input tokens consumed")
    output_tokens: int = Field(default=0, description="Output tokens consumed")
    cost: float = Field(default=0.0, description="Estimated USD cost")
    wall_time_seconds: float = Field(
        default=0.0,
        description="Accumulated in-flight time for LLM requests (sim time)",
    )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: BudgetUsage) -> BudgetUsage:
        """Return a new usage with ``other`` added (pure; no mutation)."""
        return BudgetUsage(
            request_count=self.request_count + other.request_count,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost=self.cost + other.cost,
            wall_time_seconds=self.wall_time_seconds + other.wall_time_seconds,
        )


class BudgetLimits(BaseModel):
    """Caps for one scope. A value of 0 means "unlimited" for that
    dimension (concurrency included; the per-agent concurrency cap
    falls back to ``SimulationConfig.max_concurrent_llm_requests``
    when 0 — preserving the pre-budget admission behavior)."""

    request_count: int = Field(default=0, ge=0, description="Max LLM requests")
    total_tokens: int = Field(default=0, ge=0, description="Max input+output tokens")
    cost: float = Field(default=0.0, ge=0.0, description="Max USD cost")
    wall_time_seconds: float = Field(
        default=0.0, ge=0.0, description="Max accumulated in-flight time",
    )
    concurrency: int = Field(
        default=0, ge=0, description="Max in-flight LLM requests (0=fallback/unlimited)",
    )


class BudgetConfig(BaseModel):
    """Budget configuration, exposed as ``SimulationConfig.budget``.

    Defaults are deliberately generous (safety rails, not throttles);
    deployments tighten per agent / task / simulation.
    """

    pricing: dict[str, tuple[float, float]] | None = Field(
        default=None,
        description="Model → (input $/1M, output $/1M) override of DEFAULT_PRICING",
    )
    agent: BudgetLimits = Field(
        default_factory=lambda: BudgetLimits(
            request_count=1000,
            total_tokens=10_000_000,
            cost=100.0,
            wall_time_seconds=86_400,  # 24h of accumulated in-flight time
            concurrency=0,  # fallback → SimulationConfig.max_concurrent_llm_requests
        ),
        description="Per-agent caps",
    )
    task: BudgetLimits = Field(
        default_factory=lambda: BudgetLimits(
            request_count=5000,
            total_tokens=50_000_000,
            cost=500.0,
            wall_time_seconds=604_800,  # 7d
        ),
        description="Per-task caps (shared by every agent working the task)",
    )
    simulation: BudgetLimits = Field(
        default_factory=lambda: BudgetLimits(
            request_count=100_000,
            total_tokens=1_000_000_000,
            cost=10_000.0,
            wall_time_seconds=2_592_000,  # 30d
        ),
        description="Per-simulation caps",
    )


def estimate_llm_usage(
    model: str,
    messages: Any,
    max_tokens: int,
    timeout_ticks: int,
    tick_duration_seconds: float,
    pricing: dict[str, tuple[float, float]] | None = None,
) -> BudgetUsage:
    """Deterministic usage estimate for one pending LLM request.

    Input tokens are estimated from message content (chars/4); output
    tokens use the request's ``max_tokens`` as a conservative upper
    bound; wall time uses the request's timeout (its maximum in-flight
    window). Used by PreValidate for the "累计 + 本次请求估算" check and
    as the recorded amount when a provider reports no usage.
    """
    input_tokens = estimate_input_tokens(messages)
    output_tokens = max(0, int(max_tokens))
    return BudgetUsage(
        request_count=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=estimate_cost(model, input_tokens, output_tokens, pricing),
        wall_time_seconds=max(0.0, int(timeout_ticks)) * max(0.0, tick_duration_seconds),
    )


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


@dataclass
class InFlightCounts:
    """Current in-flight LLM request counts at each scope.

    Computed by the simulation from the pending-op registry (ops in
    SUBMITTED/PENDING status) at PreValidate time.
    """

    agent: int = 0
    task: int = 0
    simulation: int = 0


@dataclass
class BudgetCheckResult:
    """The first exceeded limit, or None-carrying result if allowed."""

    exceeded: bool
    scope: str = ""
    dimension: str = ""
    current: float = 0.0
    limit: float = 0.0
    estimate: float = 0.0

    @property
    def reason(self) -> str:
        if not self.exceeded:
            return "budget_ok"
        return (
            f"{self.scope}-scope {self.dimension} budget exceeded: "
            f"cumulative {self.current:g} + estimate {self.estimate:g} "
            f"> limit {self.limit:g}"
        )


class BudgetTracker:
    """Cumulative LLM budget accounting per agent / task / simulation.

    ``record_llm`` charges a completed invocation to all three scopes;
    ``check`` answers PreValidate's question: may this round's LLM
    requests proceed given cumulative + estimate and current in-flight
    concurrency? First exceeded dimension wins.
    """

    def __init__(self, config: BudgetConfig | None = None) -> None:
        self._config = config or BudgetConfig()
        self._agent_usage: dict[str, BudgetUsage] = {}
        self._task_usage: dict[str, BudgetUsage] = {}
        self._simulation_usage = BudgetUsage()

    @property
    def config(self) -> BudgetConfig:
        return self._config

    # -- recording ----------------------------------------------------------

    def record_llm(
        self,
        agent_id: str,
        task_id: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float | None = None,
        wall_time_seconds: float = 0.0,
        pricing: dict[str, tuple[float, float]] | None = None,
    ) -> BudgetUsage:
        """Charge one completed LLM invocation to agent/task/simulation.

        ``cost`` defaults to the pricing-table estimate for the given
        token counts; pass an explicit cost to use provider-reported
        pricing instead. ``pricing`` overrides the tracker's configured
        table (used by the simulation so its config stays authoritative).
        """
        if cost is None:
            cost = estimate_cost(
                model, input_tokens, output_tokens,
                pricing if pricing is not None else self._config.pricing,
            )
        usage = BudgetUsage(
            request_count=1,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cost=max(0.0, cost),
            wall_time_seconds=max(0.0, wall_time_seconds),
        )
        agent_usage = self._agent_usage.setdefault(agent_id, BudgetUsage())
        self._agent_usage[agent_id] = agent_usage.add(usage)
        if task_id:
            task_usage = self._task_usage.setdefault(task_id, BudgetUsage())
            self._task_usage[task_id] = task_usage.add(usage)
        self._simulation_usage = self._simulation_usage.add(usage)
        return usage

    # -- queries ------------------------------------------------------------

    def agent_usage(self, agent_id: str) -> BudgetUsage:
        return self._agent_usage.get(agent_id, BudgetUsage())

    def task_usage(self, task_id: str) -> BudgetUsage:
        return self._task_usage.get(task_id, BudgetUsage())

    @property
    def simulation_usage(self) -> BudgetUsage:
        return self._simulation_usage

    def check(
        self,
        agent_id: str,
        task_id: str,
        estimate: BudgetUsage,
        in_flight: InFlightCounts,
        agent_concurrency_limit: int | None = None,
        limits: BudgetConfig | None = None,
    ) -> BudgetCheckResult | None:
        """Return the first exceeded limit, or None if the round may proceed.

        ``estimate`` is the combined estimate for ALL LLM requests in
        the round (``request_count`` = number of LLM intents). Scopes
        are checked agent → task → simulation; within a scope, cost is
        judged first (cost 为主), then tokens, then request count, then
        wall time; concurrency is an admission gate checked before the
        cumulative dimensions. ``limits`` overrides the tracker's own
        config (the simulation passes its SimulationConfig.budget so the
        config stays the single source of truth).
        """
        limits = limits or self._config
        agent_limits = limits.agent
        # Per-agent concurrency: an explicit budget cap wins; 0 falls
        # back to the caller-provided legacy cap
        # (max_concurrent_llm_requests). A legacy cap of 0 keeps its
        # legacy force-denial meaning (reject any submission); when no
        # legacy cap is supplied at all, 0 = unlimited.
        agent_concurrency = agent_limits.concurrency
        legacy_provided = agent_concurrency_limit is not None
        if agent_concurrency == 0 and legacy_provided:
            assert agent_concurrency_limit is not None
            agent_concurrency = agent_concurrency_limit

        # Concurrency is instantaneous, not cumulative: in-flight + this
        # round's requests must stay under the cap (strict, so an
        # effective cap of 0 rejects every submission).
        plan_requests = max(0, estimate.request_count)
        if (agent_concurrency > 0 or (legacy_provided and agent_concurrency == 0)) and (
            in_flight.agent + plan_requests > agent_concurrency
        ):
            return BudgetCheckResult(
                exceeded=True, scope="agent", dimension="concurrency",
                current=float(in_flight.agent), limit=float(agent_concurrency),
                estimate=float(plan_requests),
            )

        # Agent scope (cumulative)
        result = self._check_scope(
            "agent", self.agent_usage(agent_id), agent_limits, estimate,
        )
        if result is not None:
            return result

        # Task scope (cumulative) — only when the request names a task
        if task_id:
            task_limits = limits.task
            if (
                task_limits.concurrency > 0
                and in_flight.task + plan_requests > task_limits.concurrency
            ):
                return BudgetCheckResult(
                    exceeded=True, scope="task", dimension="concurrency",
                    current=float(in_flight.task), limit=float(task_limits.concurrency),
                    estimate=float(plan_requests),
                )
            result = self._check_scope(
                "task", self.task_usage(task_id), task_limits, estimate,
            )
            if result is not None:
                return result

        # Simulation scope (cumulative)
        sim_limits = limits.simulation
        if (
            sim_limits.concurrency > 0
            and in_flight.simulation + plan_requests > sim_limits.concurrency
        ):
            return BudgetCheckResult(
                exceeded=True, scope="simulation", dimension="concurrency",
                current=float(in_flight.simulation), limit=float(sim_limits.concurrency),
                estimate=float(plan_requests),
            )
        return self._check_scope(
            "simulation", self._simulation_usage, sim_limits, estimate,
        )

    @staticmethod
    def _check_scope(
        scope: str,
        usage: BudgetUsage,
        limits: BudgetLimits,
        estimate: BudgetUsage,
    ) -> BudgetCheckResult | None:
        """Cost-first cumulative check for one scope; None = allowed."""
        if limits.cost > 0 and usage.cost + estimate.cost > limits.cost:
            return BudgetCheckResult(
                exceeded=True, scope=scope, dimension="cost",
                current=usage.cost, limit=limits.cost, estimate=estimate.cost,
            )
        if (
            limits.total_tokens > 0
            and usage.total_tokens + estimate.total_tokens > limits.total_tokens
        ):
            return BudgetCheckResult(
                exceeded=True, scope=scope, dimension="total_tokens",
                current=float(usage.total_tokens), limit=float(limits.total_tokens),
                estimate=float(estimate.total_tokens),
            )
        if (
            limits.request_count > 0
            and usage.request_count + estimate.request_count > limits.request_count
        ):
            return BudgetCheckResult(
                exceeded=True, scope=scope, dimension="request_count",
                current=float(usage.request_count), limit=float(limits.request_count),
                estimate=float(estimate.request_count),
            )
        if limits.wall_time_seconds > 0 and (
            usage.wall_time_seconds + estimate.wall_time_seconds > limits.wall_time_seconds
        ):
            return BudgetCheckResult(
                exceeded=True, scope=scope, dimension="wall_time",
                current=usage.wall_time_seconds, limit=limits.wall_time_seconds,
                estimate=estimate.wall_time_seconds,
            )
        return None

    # -- persistence --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe state for ``Simulation._collect_state``."""
        return {
            "config": self._config.model_dump(mode="json"),
            "agent_usage": {
                aid: u.model_dump(mode="json")
                for aid, u in sorted(self._agent_usage.items())
            },
            "task_usage": {
                tid: u.model_dump(mode="json")
                for tid, u in sorted(self._task_usage.items())
            },
            "simulation_usage": self._simulation_usage.model_dump(mode="json"),
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Rebuild accumulators from a saved snapshot (crash recovery).

        Missing keys fall back to empty accumulators, so saves from
        before this feature loads cleanly.
        """
        if not snapshot:
            return
        saved_config = snapshot.get("config")
        if isinstance(saved_config, dict):
            self._config = BudgetConfig.model_validate(saved_config)
        self._agent_usage = {
            aid: BudgetUsage.model_validate(u)
            for aid, u in (snapshot.get("agent_usage") or {}).items()
        }
        self._task_usage = {
            tid: BudgetUsage.model_validate(u)
            for tid, u in (snapshot.get("task_usage") or {}).items()
        }
        sim = snapshot.get("simulation_usage")
        self._simulation_usage = (
            BudgetUsage.model_validate(sim) if isinstance(sim, dict) else BudgetUsage()
        )

    def __repr__(self) -> str:
        sim = self._simulation_usage
        return (
            f"BudgetTracker(simulation={sim.request_count} requests, "
            f"${sim.cost:.4f}, {sim.total_tokens} tokens)"
        )
