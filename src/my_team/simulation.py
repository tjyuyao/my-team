"""Simulation integration layer — ties all components into a runnable system.

Per SPEC §3, §8, §10:
- Combines AgentTree, MailSystem, TaskTree, SharedKB, TickEngine
- Manages AgentRuntime instances per agent
- Drives the 9-phase tick cycle (kernel model) with real agent execution
- Handles email delivery, tool execution, and state commit

Architecture (v0.6.0):
- Tick is the kernel's state commit unit, NOT the agent's ReAct cycle
- Agent uses AgentContinuation for resumable ReAct state
- External operations (LLM, tool) go through PendingOperationRegistry
- Phase 5 (Decide) produces Intents, never blocks on external calls
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from my_team.agent_runtime import (
    ActionResult,
    AgentAction,
    AgentObservation,
    AgentRuntime,
    AgentSnapshot,
    ManagerAgent,
    RootAgent,
    SubAgent,
    ToolContext,
    ToolRegistry,
    ToolResult,
    _proxy,
    _proxy_nested,
)
from my_team.agent_state import AgentState, AgentStateMachine
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType, AuditLog
from my_team.delegation import DelegationProtocol
from my_team.file_ops import FileOps, FileOpsAuditLog
from my_team.human_control import HumanControl
from my_team.mailbox import MailSystem
from my_team.models.activation import (
    ExecutionConfig,
    ReadyCandidate,
    WakeCondition,
    WakeEventType,
    WakeupEvent,
)
from my_team.models.agent import AgentConfig

# New v0.6.0 models
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.email import Email
from my_team.models.intent import (
    CompleteTaskIntent,
    DelegateIntent,
    FailTaskIntent,
    Intent,
    SendEmailIntent,
    SubmitLLMRequest,
    SubmitToolRequest,
    WaitForEventIntent,
    WritePrivateFileIntent,
)
from my_team.pending_ops import OpType, PendingOperationRegistry
from my_team.private_store import PrivateStore, PrivateStoreConfig
from my_team.reliability import TimeoutChecker
from my_team.scheduler import AgentScheduler
from my_team.shared_kb import LockManager, PermissionEngine, SharedKB
from my_team.task_tree import TaskTree
from my_team.tick_engine import TickConfig, TickEngine, TickResult
from my_team.transaction import EffectType, TransactionBuffer


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
    execution: ExecutionConfig = Field(
        default_factory=ExecutionConfig,
        description="Agent execution configuration",
    )


class AgentRuntimeState:
    """Authoritative runtime state for a single agent.

    Holds:
    - AgentStateMachine as the single source of truth for lifecycle state
    - AgentContinuation for resumable ReAct state (v0.6.0)

    The scheduler reads from this, not from its own internal state.
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.state_machine = AgentStateMachine(
            agent_id=agent_id,
            initial_state=AgentState.CREATED,
        )
        self.continuation = AgentContinuation(agent_id=agent_id)
        self.active_activation_id: str | None = None
        self.last_activation_tick: int | None = None

    @property
    def state(self) -> AgentState:
        return self.state_machine.state

    def initialize(self, tick: int = 0) -> None:
        """created → initialized → ready → idle"""
        self.state_machine.initialize(tick=tick, reason="system init")
        self.state_machine.mark_ready(tick=tick, reason="system init")
        self.state_machine.start(tick=tick, reason="system init")

    def begin_activation(self, tick: int) -> None:
        """idle/ready/waiting_for_* → processing"""
        if self.state == AgentState.IDLE:
            self.state_machine.wake_up(tick=tick, reason="scheduler activation")
        if self.state == AgentState.READY:
            self.state_machine.begin_processing(tick=tick, reason="activation start")
        elif self.state in {
            AgentState.WAITING_FOR_LLM,
            AgentState.WAITING_FOR_TOOL,
            AgentState.WAITING_FOR_CHILD,
            AgentState.WAITING_FOR_MAIL,
            AgentState.WAITING_FOR_LOCK,
            AgentState.WAITING_FOR_HUMAN,
        }:
            # Result arrived — resume the activation
            self.state_machine.transition(
                AgentState.PROCESSING,
                tick=tick,
                reason="result received, resuming activation",
            )

    def complete_activation(self, tick: int) -> None:
        """processing → idle (or waiting_for_* if agent chose to wait)"""
        if self.state == AgentState.PROCESSING:
            self.state_machine.finish_processing(tick=tick, reason="activation complete")

    def transition_to_waiting(self, waiting_state: AgentState, tick: int) -> None:
        """processing → waiting_for_*"""
        if self.state == AgentState.PROCESSING:
            self.state_machine.transition(waiting_state, tick=tick, reason="awaiting event")

    def receive_llm_result(self, result: dict[str, Any], tick: int) -> None:
        """Receive LLM result and transition to PROCESSING for next activation."""
        self.continuation.receive_llm_result(result, tick)
        # Agent will be re-activated by scheduler when LLM_RESULT event arrives

    def receive_tool_result(self, result: dict[str, Any], tick: int) -> None:
        """Receive tool result and transition to PROCESSING for next activation."""
        self.continuation.receive_tool_result(result, tick)

    def __repr__(self) -> str:
        return (
            f"AgentRuntimeState({self.agent_id}, state={self.state.value}, "
            f"phase={self.continuation.phase.value})"
        )


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

        # Transaction buffer for staged-effect commit
        self._transaction_buffer = TransactionBuffer()

        # Pending operation registry (v0.6.0 — async LLM/tool tracking)
        self._pending_ops = PendingOperationRegistry()

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

        # Timeout checker (between Phase 8 Commit and Phase 9 Publish)
        self._timeout_checker = TimeoutChecker(
            task_tree=self._task_tree,
            lock_manager=self._lock_manager,
            audit_log=self._audit_log,
        )

        # Agent scheduler (event-driven activation)
        self._scheduler = AgentScheduler(config=self._config.execution)

        # Agent runtimes
        self._runtimes: dict[str, AgentRuntime] = {}

        # Agent runtime states (authoritative state source)
        self._agent_runtime_states: dict[str, AgentRuntimeState] = {}

        # File ops
        self._file_ops = FileOps(
            private_store=self._private_store,
            audit_log=self._file_ops_audit,
        )

        # Initialize
        self._register_tool_handlers()
        self._initialize()

    def _initialize(self) -> None:
        """Set up all agents: mailboxes, private spaces, runtimes, tool registry, scheduler."""
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

            # Create and initialize agent runtime state
            runtime_state = AgentRuntimeState(agent_id=agent_id)
            runtime_state.initialize()
            self._agent_runtime_states[agent_id] = runtime_state

            # Register with scheduler — bootstrap agents wake on tick 0
            is_bootstrap = agent_config.metadata.get("bootstrap", False)
            wake_types: set[WakeEventType] = set()
            if is_bootstrap:
                wake_types.add(WakeEventType.BOOTSTRAP)
            # All agents can be woken by emails and human messages
            wake_types.update({
                WakeEventType.NEW_EMAIL,
                WakeEventType.HUMAN_MESSAGE,
                WakeEventType.TOOL_RESULT,
                WakeEventType.CHILD_TASK_CHANGE,
                WakeEventType.DEADLINE_APPROACHING,
            })
            initial_condition = WakeCondition(
                event_types=wake_types,
                wake_at_tick=0,
            )
            self._scheduler.register_agent(agent_id, initial_condition)

            # Audit
            self._audit_log.record(
                AuditEventType.AGENT_CREATED,
                agent_id=agent_id,
                details={"role": agent_config.role, "tools": list(tools)},
            )

    def _register_tool_handlers(self) -> None:
        """Register tool handlers that connect ToolRegistry to subsystems.

        Write tools (write, send_email, delegate) stage effects in the
        TransactionBuffer. Read tools (read, ls) execute directly.
        """
        # Read-only tools — execute directly
        def handle_read(context: ToolContext, path: str = "", **_kw: Any) -> Any:
            home = self._private_store.agent_home(context.agent_id)
            target = home / path
            if not target.exists() or not target.is_file():
                return ToolResult(
                    success=False, error=f"File not found: {path}",
                    agent_id=context.agent_id, tool_name="read",
                    tick=context.tick,
                )
            content = target.read_text(encoding="utf-8")
            return ToolResult(
                success=True, data={"content": content},
                agent_id=context.agent_id, tool_name="read",
                tick=context.tick,
            )

        def handle_ls(context: ToolContext, path: str = "", **_kw: Any) -> Any:
            home = self._private_store.agent_home(context.agent_id)
            target = home / path if path else home
            if not target.exists():
                return ToolResult(
                    success=False, error=f"Directory not found: {path}",
                    agent_id=context.agent_id, tool_name="ls",
                    tick=context.tick,
                )
            entries = sorted(p.name for p in target.iterdir())
            return ToolResult(
                success=True, data={"entries": entries},
                agent_id=context.agent_id, tool_name="ls",
                tick=context.tick,
            )

        def handle_write(
            context: ToolContext, path: str = "", content: str = "", **_kw: Any,
        ) -> Any:
            # Stage as a file write effect — committed in Phase 8
            self._transaction_buffer.stage(
                effect_type=EffectType.FILE_WRITE,
                agent_id=context.agent_id,
                resource=path,
                data={"content": content},
            )
            return ToolResult(
                success=True, data={"staged": True},
                agent_id=context.agent_id, tool_name="write",
                tick=context.tick,
            )

        def handle_send_email(
            context: ToolContext,
            to: list[str] | None = None,
            subject: str = "",
            body: str = "",
            **_kw: Any,
        ) -> Any:
            # Stage as an email send effect — committed in Phase 8
            self._transaction_buffer.stage(
                effect_type=EffectType.EMAIL_SEND,
                agent_id=context.agent_id,
                resource=f"email:{context.agent_id}",
                data={
                    "from_agent": context.agent_id,
                    "to": to or [],
                    "subject": subject,
                    "body": body,
                },
            )
            return ToolResult(
                success=True, data={"staged": True},
                agent_id=context.agent_id, tool_name="send_email",
                tick=context.tick,
            )

        def handle_delegate(
            context: ToolContext,
            recipient_agent_id: str = "",
            task_title: str = "",
            task_description: str = "",
            **_kw: Any,
        ) -> Any:
            # Create task + send delegation email — staged as two effects
            from uuid import uuid4
            task_id = f"task.{context.tick}.{uuid4().hex[:8]}"
            self._transaction_buffer.stage(
                effect_type=EffectType.TASK_CREATE,
                agent_id=context.agent_id,
                resource=task_id,
                data={
                    "task_id": task_id,
                    "title": task_title,
                    "description": task_description,
                    "creator_agent_id": context.agent_id,
                    "owner_agent_id": recipient_agent_id,
                    "parent_task_id": None,
                },
            )
            self._transaction_buffer.stage(
                effect_type=EffectType.EMAIL_SEND,
                agent_id=context.agent_id,
                resource=f"email:{context.agent_id}",
                data={
                    "from_agent": context.agent_id,
                    "to": [recipient_agent_id],
                    "subject": f"[DELEGATE] {task_title}",
                    "body": task_description,
                    "email_type": "delegation",
                    "task_id": task_id,
                },
            )
            return ToolResult(
                success=True,
                data={"task_id": task_id, "staged": True},
                agent_id=context.agent_id, tool_name="delegate",
                tick=context.tick,
            )

        self._tool_registry.register_handler("read", handle_read)
        self._tool_registry.register_handler("ls", handle_ls)
        self._tool_registry.register_handler("write", handle_write)
        self._tool_registry.register_handler("send_email", handle_send_email)
        self._tool_registry.register_handler("delegate", handle_delegate)

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
    def scheduler(self) -> AgentScheduler:
        return self._scheduler

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

    # -- Tick execution (10 phases) -----------------------------------------

    def run_tick(self) -> TickResult:
        """Execute one complete tick through the 9-phase kernel cycle.

        Per SPEC §8.6: Tick is the kernel's state commit unit, NOT the
        agent's ReAct cycle. Each phase is finite and non-blocking.

        Phases:
        1. Ingest   — collect completed external events (LLM, tool, human)
                      + deliver emails whose deliver_at_tick <= current_tick
        2. Freeze   — snapshot global state
        3. Schedule — compute ready set from events + agent states
        4. Resume   — restore agent continuation, read new events
        5. Decide   — generate Intents (non-blocking, no sync LLM/tool)
        6. Validate — validate Intents before execution
        7. Commit   — apply staged effects, register pending operations
        8. Publish  — dispatch pending ops, generate wake events; timeouts
        9. Audit    — record all events
        """
        tick = self._tick_engine.current_tick

        # Phase 1: Ingest — collect completed external operations + deliver emails
        self._phase_ingest(tick)
        delivered = self._phase_deliver(tick)

        # Phase 2: Freeze — snapshot global state
        snapshot = self._build_snapshot(tick)

        # Phase 3: Schedule — determine which agents activate
        ready = self._phase_schedule(tick)

        # Phase 4: Resume — restore agent continuation, read new events
        observations = self._phase_observe(tick, snapshot, ready)

        # Phase 5: Decide — generate Intents (non-blocking)
        plans = self._phase_decide(tick, observations, ready)

        # Phase 6: Validate — check Intents before execution
        validated = self._phase_validate(tick, plans, ready)

        # Phase 7: Commit — apply staged effects, register pending ops
        all_results = self._phase_act(tick, plans, ready, validated)
        self._phase_commit(tick, all_results)
        self._transaction_buffer.clear()

        # Phase 8: Publish — dispatch pending ops, generate wake events
        self._phase_publish(tick, delivered, all_results, ready)

        # Phase 9: Audit
        self._phase_audit(tick, delivered, all_results, ready)

        # Complete activations and clean up scheduler
        for candidate in ready:
            activation = self._scheduler._activations_this_tick.get(candidate.agent_id)
            if activation:
                self._scheduler.complete_activation(
                    activation.activation_id, success=True
                )
                # Transition agent state: PROCESSING → IDLE
                runtime_state = self._agent_runtime_states.get(candidate.agent_id)
                if runtime_state:
                    runtime_state.complete_activation(tick)
        self._scheduler.end_tick()

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

    def _phase_ingest(self, tick: int) -> None:
        """Phase 1: Collect completed external operations.

        Checks the PendingOperationRegistry for completed operations
        and publishes them as WakeEvents for the current tick.
        """
        # Check for timed-out operations
        expired = self._pending_ops.timeout_expired(tick)
        for op in expired:
            self._audit_log.record(
                AuditEventType.TOOL_RESULT,
                agent_id=op.agent_id,
                tick=tick,
                details={
                    "request_id": op.request_id,
                    "status": "timed_out",
                    "op_type": op.op_type.value,
                },
                success=False,
                error=f"Operation timed out at tick {tick}",
            )

        # Collect completed operations eligible for this tick
        completed = self._pending_ops.collect_completed(tick)
        for op in completed:
            # Deliver the result to the agent's continuation
            runtime_state = self._agent_runtime_states.get(op.agent_id)
            if runtime_state:
                if op.op_type == OpType.LLM_REQUEST:
                    runtime_state.receive_llm_result(op.result, tick)
                elif op.op_type == OpType.TOOL_REQUEST:
                    runtime_state.receive_tool_result(op.result, tick)

            if op.op_type == OpType.LLM_REQUEST:
                # LLM result ready → publish LLM_RESULT wake event
                self._scheduler.enqueue_event(WakeupEvent(
                    event_type=WakeEventType.TOOL_RESULT,  # reuse TOOL_RESULT for now
                    target_agent_id=op.agent_id,
                    tick=tick,
                    source_agent_id="llm_gateway",
                    task_id=op.task_id,
                    details={
                        "request_id": op.request_id,
                        "result_type": "llm_result",
                        "result": op.result,
                    },
                ))
            elif op.op_type == OpType.TOOL_REQUEST:
                # Tool result ready → publish TOOL_RESULT wake event
                self._scheduler.enqueue_event(WakeupEvent(
                    event_type=WakeEventType.TOOL_RESULT,
                    target_agent_id=op.agent_id,
                    tick=tick,
                    source_agent_id="tool_executor",
                    task_id=op.task_id,
                    details={
                        "request_id": op.request_id,
                        "result_type": "tool_result",
                        "result": op.result,
                    },
                ))

            # Remove the consumed operation so it is not re-delivered
            self._pending_ops.remove(op.request_id)

    def _get_agent_states(self) -> dict[str, AgentState]:
        """Get current state of all agents for scheduler.

        Uses AgentRuntimeState as the authoritative source.
        """
        return {
            aid: rs.state
            for aid, rs in self._agent_runtime_states.items()
        }

    def _phase_schedule(self, tick: int) -> list[ReadyCandidate]:
        """Phase 3: Compute ready set from pending events + agent states.

        For bootstrap tick (tick 0), enqueue BOOTSTRAP events for agents
        that have bootstrap=True in their config.
        """
        # Bootstrap: enqueue events on first tick
        if tick == 0:
            for agent_config in self._agent_tree:
                is_bootstrap = agent_config.metadata.get("bootstrap", False)
                if is_bootstrap:
                    self._scheduler.enqueue_event(WakeupEvent(
                        event_type=WakeEventType.BOOTSTRAP,
                        target_agent_id=agent_config.agent_id,
                        tick=tick,
                        source_agent_id="system",
                    ))

        agent_states = self._get_agent_states()
        ready = self._scheduler.compute_ready_set(tick, agent_states)

        # Begin activations for ready candidates
        for candidate in ready:
            activation = self._scheduler.begin_activation(candidate, tick)

            # Transition agent state: IDLE → READY → PROCESSING
            runtime_state = self._agent_runtime_states.get(candidate.agent_id)
            if runtime_state:
                runtime_state.begin_activation(tick)

            self._audit_log.record(
                AuditEventType.AGENT_ACTIVATED,
                agent_id=candidate.agent_id,
                tick=tick,
                details={
                    "activation_id": activation.activation_id,
                    "event_count": len(candidate.events),
                    "event_types": [e.event_type.value for e in candidate.events],
                },
            )

        return ready

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
                lock.resource: {
                    "owner": lock.owner_agent_id,
                    "lease_until": lock.lease_until_tick,
                }
                for lock in self._lock_manager.active_locks()
            },
            "lock_tokens": {
                lock.resource: lock.lock_token
                for lock in self._lock_manager.active_locks()
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
        """Phase 2: Deliver emails and generate NEW_EMAIL wake events.

        Wake events are enqueued for tick+1 visibility.
        """
        delivered = self._mail_system.deliver(tick)
        # Generate wake events for recipients — visible in tick+1
        for email in delivered:
            for recipient in email.to:
                self._scheduler.enqueue_event(WakeupEvent(
                    event_type=WakeEventType.NEW_EMAIL,
                    target_agent_id=recipient,
                    tick=tick,  # produced at tick t, visible at tick t+1
                    source_agent_id=email.from_agent,
                    task_id=email.task_id or "",
                    thread_id=email.thread_id or "",
                    details={"email_id": email.email_id},
                ))
        return delivered

    def _phase_observe(
        self,
        tick: int,
        snapshot: dict[str, Any],
        ready: list[ReadyCandidate] | None = None,
    ) -> dict[str, AgentObservation]:
        """Phase 4: Ready agents observe the frozen snapshot."""
        observations: dict[str, AgentObservation] = {}
        # Determine which agents to observe
        if ready is not None:
            active_ids = {c.agent_id for c in ready}
        else:
            active_ids = set(self._runtimes.keys())

        for agent_id, runtime in self._runtimes.items():
            if agent_id not in active_ids:
                continue
            # Filter lock tokens: only the lock holder sees their token
            agent_locks = {}
            lock_tokens = snapshot.get("lock_tokens", {})
            for resource, lock_info in snapshot["locks"].items():
                entry = dict(lock_info)
                if entry.get("owner") == agent_id and resource in lock_tokens:
                    entry["lock_token"] = lock_tokens[resource]
                agent_locks[resource] = entry

            # Build typed, deeply immutable agent-specific snapshot
            agent_snapshot = AgentSnapshot(
                tick=tick,
                emails=tuple(snapshot["emails"]),
                task_states=_proxy_nested(snapshot["tasks"]),
                shared_kb_snapshot=_proxy(snapshot["shared_kb"]),
                lock_states=_proxy(agent_locks),
                private_workspace_path=str(
                    self._private_store.agent_home(agent_id)
                ),
            )
            observations[agent_id] = runtime.observe(agent_snapshot)
        return observations

    def _phase_decide(
        self,
        tick: int,
        observations: dict[str, AgentObservation],
        ready: list[ReadyCandidate] | None = None,
    ) -> dict[str, list[Any]]:
        """Phase 5: Ready agents produce non-blocking Intents.

        v0.6.0: calls decide_intents() — the agent never blocks on
        LLM/tool calls here. Rule-based agents produce ActionPlans via
        decide() which are converted to Intents by BaseAgent.
        """
        intents: dict[str, list[Any]] = {}
        if ready is not None:
            active_ids = {c.agent_id for c in ready}
        else:
            active_ids = set(self._runtimes.keys())

        for agent_id, runtime in self._runtimes.items():
            if agent_id not in active_ids:
                continue
            obs = observations.get(agent_id)
            if obs:
                continuation = self._agent_runtime_states[agent_id].continuation
                intents[agent_id] = runtime.decide_intents(
                    obs, continuation=continuation,
                )
                # If the agent just processed a pending result, finalize
                if continuation.phase == ContinuationPhase.PROCESSING_RESULT:
                    continuation.finalize_result_processing(tick)
        return intents

    def _phase_act(
        self,
        tick: int,
        plans: dict[str, list[Intent]],
        ready: list[ReadyCandidate] | None = None,
        validated: dict[str, list[ActionResult]] | None = None,
    ) -> dict[str, list[ActionResult]]:
        """Phase 7: Convert validated Intents into staged effects.

        Only intents that passed validation in Phase 6 are processed.
        Each intent type maps to:

        - SubmitLLMRequest   → register PendingOperation, agent → WAITING_FOR_LLM
        - SubmitToolRequest  → local tools (read/ls) execute directly;
                               remote tools register PendingOperation
        - SendEmailIntent    → stage EMAIL_SEND effect
        - DelegateIntent     → stage TASK_CREATE + EMAIL_SEND effects
        - WritePrivateFileIntent → stage FILE_WRITE effect
        - WaitForEventIntent → agent → waiting state
        """
        all_results: dict[str, list[ActionResult]] = {}

        for agent_id, intent_list in plans.items():
            # Determine which intents passed validation
            validated_idx: set[int] = set()
            if validated and agent_id in validated:
                for i, vr in enumerate(validated[agent_id]):
                    if vr.success:
                        validated_idx.add(i)

            results: list[ActionResult] = []
            runtime_state = self._agent_runtime_states.get(agent_id)

            for i, intent in enumerate(intent_list):
                action = AgentAction(
                    action_type=intent.intent_type.value,
                    tool_name=getattr(intent, "tool_name", ""),
                    payload=dict(intent.payload),
                )

                if i not in validated_idx:
                    # Validation failure — record from validate phase
                    if validated and agent_id in validated and i < len(validated[agent_id]):
                        results.append(validated[agent_id][i])
                    continue

                # SubmitLLMRequest → async LLM registration
                if isinstance(intent, SubmitLLMRequest):
                    op = self._pending_ops.submit(
                        op_type=OpType.LLM_REQUEST,
                        agent_id=agent_id,
                        created_tick=tick,
                        eligible_tick=tick + 1,
                        deadline_tick=tick + intent.timeout_ticks,
                        task_id=intent.task_id,
                        metadata={
                            "request_id": intent.request_id,
                            "model": intent.model,
                        },
                    )
                    if runtime_state:
                        runtime_state.continuation.advance_to_waiting_llm(
                            op.request_id, tick,
                        )
                        runtime_state.transition_to_waiting(
                            AgentState.WAITING_FOR_LLM, tick,
                        )
                    results.append(ActionResult(
                        action=action,
                        success=True,
                        result_data={
                            "request_id": op.request_id,
                            "status": "pending",
                        },
                    ))
                    continue

                # SubmitToolRequest → local tools execute, remote register
                if isinstance(intent, SubmitToolRequest):
                    if intent.tool_name in {"read", "ls"}:
                        # Local tools execute synchronously
                        runtime = self._runtimes.get(agent_id)
                        if runtime:
                            tool_context = ToolContext(
                                agent_id=agent_id,
                                tick=tick,
                                allowed_tools=self._tool_context_allowed(agent_id),
                            )
                            tr = self._tool_registry.execute(
                                context=tool_context,
                                tool_name=intent.tool_name,
                                **intent.arguments,
                            )
                            results.append(ActionResult(
                                action=action,
                                success=tr.success,
                                result_data=tr.data,
                                error=tr.error,
                            ))
                        else:
                            results.append(ActionResult(
                                action=action, success=False,
                                error=f"No runtime for '{agent_id}'",
                            ))
                    else:
                        # Remote tool → register pending operation
                        op = self._pending_ops.submit(
                            op_type=OpType.TOOL_REQUEST,
                            agent_id=agent_id,
                            created_tick=tick,
                            eligible_tick=tick + 1,
                            deadline_tick=tick + intent.timeout_ticks,
                            task_id=intent.task_id,
                            metadata={"request_id": intent.request_id},
                        )
                        if runtime_state:
                            runtime_state.continuation.advance_to_waiting_tool(
                                op.request_id, tick,
                            )
                            runtime_state.transition_to_waiting(
                                AgentState.WAITING_FOR_TOOL, tick,
                            )
                        results.append(ActionResult(
                            action=action,
                            success=True,
                            result_data={
                                "request_id": op.request_id,
                                "status": "pending",
                            },
                        ))
                    continue

                # SendEmailIntent → stage EMAIL_SEND
                if isinstance(intent, SendEmailIntent):
                    self._transaction_buffer.stage(
                        effect_type=EffectType.EMAIL_SEND,
                        agent_id=agent_id,
                        resource=f"email:{agent_id}",
                        data={
                            "from_agent": agent_id,
                            "to": intent.to,
                            "subject": intent.subject,
                            "body": intent.body,
                            "email_type": intent.email_type,
                            "task_id": intent.task_id,
                        },
                    )
                    results.append(ActionResult(
                        action=action, success=True,
                        result_data={"staged": True},
                    ))
                    continue

                # DelegateIntent → stage TASK_CREATE + EMAIL_SEND
                if isinstance(intent, DelegateIntent):
                    from uuid import uuid4
                    task_id = f"task.{tick}.{uuid4().hex[:8]}"
                    self._transaction_buffer.stage(
                        effect_type=EffectType.TASK_CREATE,
                        agent_id=agent_id,
                        resource=task_id,
                        data={
                            "task_id": task_id,
                            "title": intent.task_title,
                            "description": intent.task_description,
                            "creator_agent_id": agent_id,
                            "owner_agent_id": intent.recipient_agent_id,
                            "parent_task_id": intent.parent_task_id or None,
                        },
                    )
                    self._transaction_buffer.stage(
                        effect_type=EffectType.EMAIL_SEND,
                        agent_id=agent_id,
                        resource=f"email:{agent_id}",
                        data={
                            "from_agent": agent_id,
                            "to": [intent.recipient_agent_id],
                            "subject": f"[DELEGATE] {intent.task_title}",
                            "body": intent.task_description,
                            "email_type": "delegation",
                            "task_id": task_id,
                        },
                    )
                    results.append(ActionResult(
                        action=action, success=True,
                        result_data={"task_id": task_id, "staged": True},
                    ))
                    continue

                # WritePrivateFileIntent → stage FILE_WRITE
                if isinstance(intent, WritePrivateFileIntent):
                    self._transaction_buffer.stage(
                        effect_type=EffectType.FILE_WRITE,
                        agent_id=agent_id,
                        resource=intent.path,
                        data={"content": intent.content},
                    )
                    results.append(ActionResult(
                        action=action, success=True,
                        result_data={"staged": True},
                    ))
                    continue

                # WaitForEventIntent → agent waits for specific event
                if isinstance(intent, WaitForEventIntent):
                    if runtime_state:
                        waiting_state = AgentState(intent.waiting_state)
                        runtime_state.transition_to_waiting(waiting_state, tick)
                    results.append(ActionResult(
                        action=action, success=True,
                        result_data={"waiting": intent.waiting_state},
                    ))
                    continue

                # CompleteTaskIntent → stage TASK_UPDATE (completed)
                if isinstance(intent, CompleteTaskIntent):
                    self._transaction_buffer.stage(
                        effect_type=EffectType.TASK_UPDATE,
                        agent_id=agent_id,
                        resource=intent.task_id,
                        data={
                            "status": "completed",
                            "summary": intent.summary,
                            "artifacts": intent.artifacts,
                        },
                    )
                    results.append(ActionResult(
                        action=action, success=True,
                        result_data={"task_id": intent.task_id, "staged": True},
                    ))
                    continue

                # FailTaskIntent → stage TASK_UPDATE (failed)
                if isinstance(intent, FailTaskIntent):
                    self._transaction_buffer.stage(
                        effect_type=EffectType.TASK_UPDATE,
                        agent_id=agent_id,
                        resource=intent.task_id,
                        data={
                            "status": "failed",
                            "reason": intent.reason,
                            "retryable": intent.retryable,
                        },
                    )
                    results.append(ActionResult(
                        action=action, success=True,
                        result_data={"task_id": intent.task_id, "staged": True},
                    ))
                    continue

                # Unknown intent — record as failure
                results.append(ActionResult(
                    action=action,
                    success=False,
                    error=f"Unsupported intent type: {intent.intent_type}",
                ))

            all_results[agent_id] = results

        return all_results

    def _tool_context_allowed(self, agent_id: str) -> frozenset[str]:
        """Get allowed tools for an agent (helper for intent execution)."""
        return self._tool_registry.get_allowed_tools(agent_id)

    def _phase_validate(
        self,
        tick: int,
        plans: dict[str, list[Intent]],
        ready: list[ReadyCandidate],
    ) -> dict[str, list[ActionResult]]:
        """Phase 6: Validate Intents before execution.

        Checks performed:
        1. Tool capability — SubmitToolRequest tool is in agent's allowed tools
        2. Delegation target — DelegateIntent targets direct children
        3. Payload fields — WritePrivateFileIntent has path, SendEmailIntent has to
        4. Activation budget — total intents within limit

        Invalid intents are converted to failed Results. Valid intents pass through.
        """
        validated: dict[str, list[ActionResult]] = {}

        for candidate in ready:
            agent_id = candidate.agent_id
            intent_list = plans.get(agent_id)
            if intent_list is None:
                continue

            allowed_tools = self._tool_registry.get_allowed_tools(agent_id)
            results: list[ActionResult] = []

            for intent in intent_list:
                # Build a synthetic AgentAction for the result record
                action = AgentAction(
                    action_type=intent.intent_type.value,
                    tool_name=getattr(intent, "tool_name", ""),
                    payload=dict(intent.payload),
                )

                # Check 1: tool capability (SubmitToolRequest)
                if isinstance(intent, SubmitToolRequest):
                    if intent.tool_name not in allowed_tools:
                        results.append(ActionResult(
                            action=action,
                            success=False,
                            error=(
                                f"Tool '{intent.tool_name}' not authorized "
                                f"for '{agent_id}'"
                            ),
                        ))
                        self._audit_log.record(
                            AuditEventType.PERMISSION_DENIED,
                            agent_id=agent_id,
                            tick=tick,
                            details={"tool": intent.tool_name, "intent": intent.intent_type.value},
                            success=False,
                            error="Tool not authorized",
                        )
                        continue

                # Check 2: delegation target validation
                if isinstance(intent, DelegateIntent):
                    target_id = intent.recipient_agent_id
                    if not self._agent_tree.can_delegate_to(agent_id, target_id):
                        results.append(ActionResult(
                            action=action,
                            success=False,
                            error=(
                                f"'{agent_id}' cannot delegate to '{target_id}'"
                                " (not a direct child)"
                            ),
                        ))
                        continue

                # Check 3: required payload fields
                if isinstance(intent, WritePrivateFileIntent):
                    if not intent.path:
                        results.append(ActionResult(
                            action=action,
                            success=False,
                            error="write intent requires 'path' field",
                        ))
                        continue

                if isinstance(intent, SendEmailIntent):
                    if not intent.to:
                        results.append(ActionResult(
                            action=action,
                            success=False,
                            error="send_email intent requires 'to' field",
                        ))
                        continue

                if isinstance(intent, DelegateIntent):
                    if not intent.recipient_agent_id or not intent.task_title:
                        results.append(ActionResult(
                            action=action,
                            success=False,
                            error=(
                                "delegate intent requires 'recipient_agent_id' "
                                "and 'task_title' fields"
                            ),
                        ))
                        continue

                # Passed validation — will be staged in Act phase
                results.append(ActionResult(
                    action=action,
                    success=True,
                    result_data={"validated": True},
                ))

            validated[agent_id] = results

        return validated

    def _phase_publish(
        self,
        tick: int,
        delivered: list[Email],
        all_results: dict[str, list[ActionResult]],
        ready: list[ReadyCandidate],
    ) -> None:
        """Phase 9: Generate wake events from committed effects; timeout checks.

        Events generated here are only visible in tick+1.
        """
        # Timeout checks
        self._timeout_checker.check_task_timeouts(tick)
        self._timeout_checker.check_lock_timeouts(tick)

    def _phase_commit(
        self, tick: int, all_results: dict[str, list[ActionResult]]
    ) -> list[Email]:
        """Phase 8: Commit staged effects atomically.

        1. Validate effects (version, lock, permission checks)
        2. Resolve conflicts (deterministic, by agent_id)
        3. Commit all validated effects
        4. Apply committed effects to subsystems
        5. On failure, rollback
        """
        buffer = self._transaction_buffer

        # Step 1: Validate
        def check_version(resource: str, expected: int) -> bool:
            current = self._shared_kb.versions.get_version(resource)
            return current == expected

        def check_lock(
            resource: str, agent_id: str, lock_token: str | None = None,
        ) -> bool:
            # Private workspace writes don't need locks.
            # Only enforce lock checks for shared KB resources.
            if resource.startswith(("shared-kb/", "project/")):
                lock = self._lock_manager.get_lock(resource)
                if lock is None or lock.owner_agent_id != agent_id:
                    return False
                # If the effect carries a lock_token, verify it matches
                if lock_token and lock.lock_token != lock_token:
                    return False
                return True
            return True

        def check_permission(agent_id: str, resource: str, op: str) -> bool:
            # Private workspace writes don't need KB permission checks.
            # Only check permissions for shared KB operations.
            if op in {"kb_write", "kb_create", "kb_delete"}:
                return self._permission_engine.check(
                    principal=agent_id, path=resource, operation=op,
                )
            return True

        buffer.validate(
            check_version=check_version,
            check_lock=check_lock,
            check_permission=check_permission,
        )

        # Step 2: Resolve conflicts
        buffer.resolve_conflicts()

        # Step 3: Commit
        committed = buffer.commit()

        # Step 4: Apply committed effects to subsystems
        committed_emails: list[Email] = []
        for effect in committed:
            if effect.effect_type == EffectType.FILE_WRITE:
                # Write to private workspace
                agent_id = effect.agent_id
                path = effect.resource
                content = effect.data.get("content", "")
                home = self._private_store.agent_home(agent_id)
                target = home / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            elif effect.effect_type == EffectType.EMAIL_SEND:
                from my_team.models.email import EmailType
                data = effect.data
                email = self._mail_system.create_email(
                    from_agent=data.get("from_agent", effect.agent_id),
                    to=data.get("to", []),
                    subject=data.get("subject", ""),
                    body=data.get("body", ""),
                    email_type=EmailType(data.get("email_type", "progress")),
                    tick=tick,
                    deliver_at_tick=tick + self._config.email_delivery_latency_ticks,
                    task_id=data.get("task_id", ""),
                )
                committed_emails.append(email)

            elif effect.effect_type == EffectType.TASK_CREATE:
                from my_team.models.task import TaskPriority, TaskStatus
                data = effect.data
                self._task_tree.create(
                    task_id=data.get("task_id", effect.resource),
                    title=data.get("title", ""),
                    description=data.get("description", ""),
                    creator_agent_id=data.get("creator_agent_id", effect.agent_id),
                    owner_agent_id=data.get("owner_agent_id", ""),
                    parent_task_id=data.get("parent_task_id"),
                    priority=TaskPriority.NORMAL,
                    status=TaskStatus.ASSIGNED,
                    tick=tick,
                )

            elif effect.effect_type == EffectType.TASK_UPDATE:
                from my_team.models.task import TaskStatus
                data = effect.data
                task_id = effect.resource
                if self._task_tree.exists(task_id):
                    new_status = TaskStatus(data.get("status", "in_progress"))
                    self._task_tree.update_status(
                        task_id, new_status, tick=tick, allow_walk=True,
                    )
                    task = self._task_tree.get(task_id)
                    if data.get("summary"):
                        task.metadata["summary"] = data["summary"]
                    if data.get("artifacts"):
                        task.metadata["artifacts"] = data["artifacts"]

        # Record audit for committed effects
        for effect in committed:
            self._audit_log.record(
                AuditEventType.TRANSACTION_COMMIT,
                agent_id=effect.agent_id,
                tick=tick,
                details={
                    "effect_id": effect.effect_id,
                    "effect_type": effect.effect_type.value,
                    "resource": effect.resource,
                },
            )

        return committed_emails

    def _phase_audit(
        self,
        tick: int,
        delivered: list[Email],
        all_results: dict[str, list[ActionResult]],
        ready: list[ReadyCandidate] | None = None,
    ) -> None:
        """Phase 10: Record audit events for this tick."""
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
