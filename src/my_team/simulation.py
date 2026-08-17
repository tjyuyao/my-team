"""Simulation integration layer — ties all components into a runnable system.

Per SPEC §3, §8, §10:
- Combines AgentTree, MailSystem, TaskTree, SharedKB, TickEngine
- Manages AgentRuntime instances per agent
- Drives the 7-phase tick cycle with real agent execution
- Handles email delivery, tool execution, and state commit
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from my_team.agent_runtime import (
    AgentObservation,
    AgentRuntime,
    ActionResult,
    ActionContext,
    ActionPlan,
    BaseAgent,
    ManagerAgent,
    RootAgent,
    SubAgent,
    ToolContext,
    ToolRegistry,
)
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType, AuditLog
from my_team.delegation import DelegationProtocol
from my_team.file_ops import FileOps, FileOpsAuditLog
from my_team.human_control import HumanControl
from my_team.mailbox import MailSystem
from my_team.models.agent import AgentConfig
from my_team.models.email import Email, EmailType
from my_team.private_store import PrivateStore, PrivateStoreConfig
from my_team.shared_kb import LockManager, PermissionEngine, PermissionRule, SharedKB
from my_team.task_tree import TaskTree
from my_team.tick_engine import TickEngine, TickConfig, TickPhase, TickResult, TickSnapshot


class SimulationConfig(BaseModel):
    """Configuration for a simulation run."""

    name: str = Field(default="default", description="Simulation name")
    tick_duration_value: int = Field(default=10)
    tick_duration_unit: str = Field(default="seconds")
    simulation_time_per_tick_value: int = Field(default=1)
    simulation_time_per_tick_unit: str = Field(default="hour")
    start_paused: bool = Field(default=False)
    deterministic_mode: bool = Field(default=True)
    max_delegation_depth: int = Field(default=5)
    email_delivery_latency_ticks: int = Field(default=1)
    default_lock_lease_ticks: int = Field(default=4)
    max_retries: int = Field(default=3)
    private_storage_limit_mb: int = Field(default=512)


class Simulation:
    """Complete simulation that integrates all components.

    Usage:
        sim = Simulation.from_config_file("configs/sample-team.json")
        results = sim.run(max_ticks=10)
    """

    def __init__(
        self,
        agent_tree: AgentTree,
        config: SimulationConfig | None = None,
    ) -> None:
        self._config = config or SimulationConfig()
        self._agent_tree = agent_tree

        # Initialize core subsystems
        self._mail_system = MailSystem()
        self._task_tree = TaskTree()
        self._audit_log = AuditLog()
        self._file_ops_audit = FileOpsAuditLog()
        self._private_store = PrivateStore(PrivateStoreConfig(
            base_path="private",
            max_storage_bytes=self._config.private_storage_limit_mb * 1024 * 1024,
        ))

        # Shared KB with permissions
        self._permission_engine = PermissionEngine()
        self._lock_manager = LockManager(
            default_lease_ticks=self._config.default_lock_lease_ticks,
        )
        self._shared_kb = SharedKB(
            permissions=self._permission_engine,
            lock_manager=self._lock_manager,
        )

        # Tool registry
        self._tool_registry = ToolRegistry()

        # Tick engine
        self._tick_engine = TickEngine(TickConfig(
            tick_duration_value=self._config.tick_duration_value,
            tick_duration_unit=self._config.tick_duration_unit,
            simulation_time_per_tick_value=self._config.simulation_time_per_tick_value,
            simulation_time_per_tick_unit=self._config.simulation_time_per_tick_unit,
            start_paused=self._config.start_paused,
            deterministic_mode=self._config.deterministic_mode,
        ))

        # Human control
        self._human_control = HumanControl(
            tick_engine=self._tick_engine,
            agent_tree=self._agent_tree,
            task_tree=self._task_tree,
            mail_system=self._mail_system,
            shared_kb=self._shared_kb,
            audit_log=self._audit_log,
        )

        # Delegation protocol
        self._delegation = DelegationProtocol(
            agent_tree=self._agent_tree,
            task_tree=self._task_tree,
            mail_system=self._mail_system,
            max_delegation_depth=self._config.max_delegation_depth,
        )

        # Agent runtimes
        self._runtimes: dict[str, AgentRuntime] = {}

        # File ops
        self._file_ops = FileOps(
            private_store=self._private_store,
            audit_log=self._file_ops_audit,
        )

        # Initialize
        self._initialize()

    def _initialize(self) -> None:
        """Set up all agents: mailboxes, private spaces, runtimes, tool registry."""
        for agent_config in self._agent_tree:
            agent_id = agent_config.agent_id

            # Register mailbox
            self._mail_system.register_agent(agent_id)

            # Create private workspace
            self._private_store.initialize_agent(agent_id)

            # Register tools based on agent role
            tools = frozenset(agent_config.tools)
            self._tool_registry.register_agent(agent_id, tools)

            # Create agent runtime
            runtime = self._create_runtime(agent_config)
            self._runtimes[agent_id] = runtime

            # Audit
            self._audit_log.record(
                AuditEventType.AGENT_CREATED,
                agent_id=agent_id,
                details={"role": agent_config.role, "tools": list(tools)},
            )

    def _create_runtime(self, config: AgentConfig) -> AgentRuntime:
        """Create an appropriate runtime for an agent based on config."""
        if config.role == "root_decision_agent":
            return RootAgent(
                agent_id=config.agent_id,
                tool_registry=self._tool_registry,
            )
        elif config.can_delegate:
            return ManagerAgent(
                agent_id=config.agent_id,
                tool_registry=self._tool_registry,
            )
        else:
            return SubAgent(
                agent_id=config.agent_id,
                tool_registry=self._tool_registry,
            )

    # -- Public API ---------------------------------------------------------

    @property
    def config(self) -> SimulationConfig:
        return self._config

    @property
    def agent_tree(self) -> AgentTree:
        return self._agent_tree

    @property
    def task_tree(self) -> TaskTree:
        return self._task_tree

    @property
    def mail_system(self) -> MailSystem:
        return self._mail_system

    @property
    def shared_kb(self) -> SharedKB:
        return self._shared_kb

    @property
    def tick_engine(self) -> TickEngine:
        return self._tick_engine

    @property
    def human_control(self) -> HumanControl:
        return self._human_control

    @property
    def delegation(self) -> DelegationProtocol:
        return self._delegation

    @property
    def audit_log(self) -> AuditLog:
        return self._audit_log

    @property
    def current_tick(self) -> int:
        return self._tick_engine.current_tick

    @classmethod
    def from_config_file(cls, path: str | Path) -> Simulation:
        """Create a simulation from a JSON config file."""
        config_path = Path(path)
        with open(config_path) as f:
            data = json.load(f)

        sim_config = SimulationConfig(**data.get("simulation", {}))
        agent_tree = AgentTree.from_dict(data)

        return cls(agent_tree=agent_tree, config=sim_config)

    # -- Tick execution (7 phases) ------------------------------------------

    def run_tick(self) -> TickResult:
        """Execute one complete tick through all 7 phases.

        This is the core integration point where all subsystems interact.
        """
        tick = self._tick_engine.current_tick

        # Phase 1: Freeze — snapshot global state
        snapshot = self._build_snapshot(tick)

        # Phase 2: Deliver — deliver emails
        delivered = self._phase_deliver(tick)

        # Phase 3: Observe — each agent reads from snapshot
        observations = self._phase_observe(tick, snapshot)

        # Phase 4: Decide — each agent generates action plan
        plans = self._phase_decide(tick, observations)

        # Phase 5: Act — execute actions through tool registry
        all_results = self._phase_act(tick, plans)

        # Phase 6: Commit — atomic state update
        committed_emails = self._phase_commit(tick, all_results)

        # Phase 7: Audit
        self._phase_audit(tick, delivered, all_results)

        # Advance tick engine
        results = self._tick_engine.advance(1)
        return results[0] if results else TickResult(
            tick=tick,
            phases_completed=[],
        )

    def run(self, max_ticks: int = 100) -> list[TickResult]:
        """Run the simulation for a given number of ticks."""
        results: list[TickResult] = []
        for _ in range(max_ticks):
            if self._tick_engine.state.value == "paused":
                break
            result = self.run_tick()
            results.append(result)
        return results

    # -- Phase implementations ----------------------------------------------

    def _build_snapshot(self, tick: int) -> dict[str, Any]:
        """Build a frozen snapshot of the global state."""
        agent_states: dict[str, dict[str, Any]] = {}
        for agent_config in self._agent_tree:
            agent_id = agent_config.agent_id
            mailbox = self._mail_system.get_mailbox(agent_id)
            owner_tasks = self._task_tree.get_owner_tasks(agent_id)

            agent_states[agent_id] = {
                "config": agent_config.model_dump(),
                "inbox_unread": mailbox.unread_count if mailbox else 0,
                "tasks": {
                    t.task_id: {
                        "status": t.status.value,
                        "title": t.title,
                        "owner": t.owner_agent_id,
                    }
                    for t in owner_tasks
                },
            }

        # Get pending emails
        pending_emails = []
        for mailbox in [self._mail_system.get_mailbox(aid) for aid in self._agent_tree.all_ids]:
            if mailbox:
                for email in mailbox.inbox:
                    if email.status.value == "delivered":
                        pending_emails.append({
                            "email_id": email.email_id,
                            "from": email.from_agent,
                            "to": email.to,
                            "subject": email.subject,
                            "email_type": email.email_type.value,
                            "task_id": email.task_id,
                            "body": email.body,
                        })

        return {
            "tick": tick,
            "agents": agent_states,
            "emails": pending_emails,
            "shared_kb": {
                "paths": self._shared_kb.all_paths(),
                "versions": {
                    p: v.version
                    for p, v in self._shared_kb.versions.all_versions().items()
                },
            },
            "locks": {
                l.resource: {
                    "owner": l.owner_agent_id,
                    "lease_until": l.lease_until_tick,
                }
                for l in self._lock_manager.active_locks()
            },
            "tasks": {
                t.task_id: {
                    "status": t.status.value,
                    "title": t.title,
                    "owner": t.owner_agent_id,
                    "creator": t.creator_agent_id,
                }
                for t in self._task_tree
            },
        }

    def _phase_deliver(self, tick: int) -> list[Email]:
        """Phase 2: Deliver emails whose deliver_at_tick <= current_tick."""
        return self._mail_system.deliver(tick)

    def _phase_observe(
        self, tick: int, snapshot: dict[str, Any]
    ) -> dict[str, AgentObservation]:
        """Phase 3: Each agent observes the frozen snapshot."""
        observations: dict[str, AgentObservation] = {}
        for agent_id, runtime in self._runtimes.items():
            # Build agent-specific snapshot
            agent_snapshot = {
                "tick": tick,
                "emails": {
                    agent_id: snapshot["emails"]
                },
                "tasks": snapshot["tasks"],
                "shared_kb": snapshot["shared_kb"],
                "locks": snapshot["locks"],
                "private_spaces": {
                    agent_id: str(self._private_store.agent_home(agent_id))
                },
            }
            observations[agent_id] = runtime.observe(agent_snapshot)
        return observations

    def _phase_decide(
        self, tick: int, observations: dict[str, AgentObservation]
    ) -> dict[str, ActionPlan]:
        """Phase 4: Each agent generates an action plan."""
        plans: dict[str, ActionPlan] = {}
        for agent_id, runtime in self._runtimes.items():
            obs = observations.get(agent_id)
            if obs:
                plans[agent_id] = runtime.decide(obs)
        return plans

    def _phase_act(
        self, tick: int, plans: dict[str, ActionPlan]
    ) -> dict[str, list[ActionResult]]:
        """Phase 5: Execute actions through tool registry."""
        all_results: dict[str, list[ActionResult]] = {}
        for agent_id, plan in plans.items():
            runtime = self._runtimes.get(agent_id)
            if runtime and plan.actions:
                context = ActionContext(
                    agent_id=agent_id,
                    tick=tick,
                    tool_context=ToolContext(
                        agent_id=agent_id,
                        tick=tick,
                        allowed_tools=self._tool_registry.get_allowed_tools(agent_id),
                    ),
                )
                results = runtime.act(plan, context)
                all_results[agent_id] = results
        return all_results

    def _phase_commit(
        self, tick: int, all_results: dict[str, list[ActionResult]]
    ) -> list[Email]:
        """Phase 6: Commit staged effects.

        Currently handles email queueing from actions.
        Full transaction model TODO (review gap §8.4).
        """
        committed_emails: list[Email] = []
        for agent_id, results in all_results.items():
            for result in results:
                if result.success and result.action.action_type == "send_email":
                    # Email was already queued by the action handler
                    pass
        return committed_emails

    def _phase_audit(
        self,
        tick: int,
        delivered: list[Email],
        all_results: dict[str, list[ActionResult]],
    ) -> None:
        """Phase 7: Record audit events for this tick."""
        # Record delivered emails
        for email in delivered:
            self._audit_log.record(
                AuditEventType.EMAIL_DELIVERED,
                agent_id=email.from_agent,
                tick=tick,
                details={
                    "email_id": email.email_id,
                    "to": email.to,
                    "email_type": email.email_type.value,
                },
            )

        # Record action results
        for agent_id, results in all_results.items():
            for result in results:
                if not result.success:
                    self._audit_log.record(
                        AuditEventType.TOOL_CALL,
                        agent_id=agent_id,
                        tick=tick,
                        details={
                            "tool": result.action.tool_name,
                            "action_type": result.action.action_type,
                        },
                        success=False,
                        error=result.error,
                    )

        # Record tick completion
        self._audit_log.record(
            AuditEventType.TICK_COMPLETE,
            tick=tick,
            details={
                "emails_delivered": len(delivered),
                "agents_with_actions": len(all_results),
            },
        )
