"""Simulation integration layer — ties all components into a runnable system.

Per SPEC §3, §8, §10:
- Combines AgentTree, MailSystem, TaskTree, SharedKB, TickEngine
- Manages AgentRuntime instances per agent
- Drives the 10-phase tick cycle (kernel model) with real agent execution
- Handles email delivery, tool execution, and state commit

Architecture (v0.6.0):
- Tick is the kernel's state commit unit, NOT the agent's ReAct cycle
- Agent uses AgentContinuation for resumable ReAct state
- External operations (LLM, tool) go through PendingOperationRegistry
- Phase 5 (Decide) produces Intents, never blocks on external calls

v0.11（N1b，§5.1）：工具权限不再按 role/白名单分配——Simulation 持有
每 Team 唯一的 Authority（注册中心 + 布线中心）；``_initialize`` 建立
**默认初始授予集**（引导 = org 初始化 + 初始授予集：每 agent 以自身为
position，基础工具 + config 声明的工具授予该 position），4 处白名单点
（Act ToolContext 构造 / Validate 按名检查 / dispatch 工具上下文）全部
改为 Authority 两层 Grant 求值（§3.5：∃position：Grant(agent, position)
∧ Grant(position, entity_id) ∧ 锁）；锁约束由工具 handler 路径叠加
（T20 写即自动锁 / kb_write 显式锁，见各 handler）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from my_team.agent_runtime import (
    ActionResult,
    AgentAction,
    AgentObservation,
    AgentRuntime,
    HumanWorkerRuntime,
    ManagerAgent,
    RootAgent,
    SubAgent,
    ToolContext,
    ToolRegistry,
)
from my_team.agent_state import AgentState, AgentStateMachine
from my_team.agent_tree import AgentTree
from my_team.asset_store import AssetStore, AttachmentRef
from my_team.audit import AuditEntry, AuditEventType, AuditLog
from my_team.budget import (
    BudgetCheckResult,
    BudgetConfig,
    BudgetTracker,
    BudgetUsage,
    InFlightCounts,
    estimate_llm_usage,
)
from my_team.calendar import CalendarStore, ScheduleAction, ScheduleRule
from my_team.consolidation import MEMORY_TOOL_NAMES, ConsolidationGate
from my_team.context_compiler import ContextCompiler
from my_team.credential_store import CredentialStore
from my_team.devices.authority import Authority, new_team_id
from my_team.executor_registry import (
    ExecutorRegistry,
    ExecutorTier,
    requires_executor,
)
from my_team.file_ops import FileOpsAuditEntry, FileOpsAuditLog
from my_team.human_control import HumanControl
from my_team.ingress import (
    IngressBuffer,
    IngressEvent,
    restore_ingress_buffer,
    snapshot_ingress_buffer,
)
from my_team.integration import (
    Integration,
    IntegrationRegistry,
)
from my_team.journal import (
    EffectSummary,
    IntentSummary,
    OutboxSummary,
    PendingOpSummary,
    TickJournal,
    TickRecordStatus,
)
from my_team.mailbox import MailSystem
from my_team.memory_recall import RecallConfig, RecallEngine
from my_team.memory_store import AgentMemory
from my_team.models.activation import (
    AgentActivation,
    ExecutionConfig,
    ReadyCandidate,
    WakeCondition,
    WakeEventType,
    WakeupEvent,
)
from my_team.models.agent import AgentConfig, PoolMode

# New v0.6.0 models
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.email import Email
from my_team.models.intent import (
    AcceptTaskIntent,
    CompleteTaskIntent,
    DelegateIntent,
    FailTaskIntent,
    Intent,
    MemoryConsolidateIntent,
    SendEmailIntent,
    SubmitLLMRequest,
    SubmitToolRequest,
    WaitForEventIntent,
    WritePrivateFileIntent,
)
from my_team.models.task import Task, TaskPriority, TaskStatus
from my_team.outbox import Outbox, OutboxEntry, OutboxStatus
from my_team.pending_ops import (
    CancellationResult,
    OpStatus,
    OpType,
    PendingOperation,
    PendingOperationRegistry,
)
from my_team.persistence import SCHEMA_VERSION, SimulationStore
from my_team.private_store import (
    AccessDeniedError,
    PrivateStore,
    PrivateStoreConfig,
)
from my_team.record_store import RecordInvariantError, RecordStore
from my_team.reliability import CrashGuard, CrashReport, TimeoutChecker
from my_team.scheduler import AgentScheduler, QueuedEvent
from my_team.shared_kb import (
    LockInfo,
    LockManager,
    PermissionEngine,
    PermissionRule,
    SharedKB,
    SharedKBResource,
    VersionInfo,
)
from my_team.task_tree import InvalidTransitionError, TaskTree
from my_team.tick_engine import SimulationState, TickConfig, TickEngine, TickResult
from my_team.tool_manifest import (
    OperationPolicy,
    ToolManifest,
    builtin_manifests,
)
from my_team.tool_protocol import ToolRequest, ToolResultContract, hash_payload
from my_team.transaction import (
    INVERT_CONTRACT,
    EffectStatus,
    EffectType,
    InvertKind,
    StagedEffect,
    TransactionBuffer,
)
from pydantic import BaseModel, Field


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
    max_concurrent_llm_requests: int = Field(
        default=4,
        ge=1,
        description="Per-agent cap on in-flight LLM requests "
        "(defense-in-depth; agents naturally wait while one is pending)",
    )
    private_storage_limit_mb: int = Field(default=512)
    # T16c: LLM usage budget (SPEC §14) — pricing table + per-agent /
    # per-task / per-simulation caps on request_count / token / cost /
    # wall_time / concurrency. Over-limit → PreValidate rejects the
    # WHOLE activation round (non-retryable; no state change). The
    # per-agent concurrency cap falls back to
    # max_concurrent_llm_requests when budget.agent.concurrency == 0.
    budget: BudgetConfig = Field(
        default_factory=BudgetConfig,
        description="LLM usage budget (pricing + limits per scope)",
    )
    # Crash guard (T19): sliding window of kernel-level crashes; crossing
    # the threshold auto-pauses the system (reason=crash_guard) after
    # notifying Provider/Owner callbacks.
    crash_guard_window_ticks: int = Field(default=10, ge=1)
    crash_guard_threshold: int = Field(default=3, ge=1)
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

    def receive_external_result(self, result: dict[str, Any], tick: int) -> None:
        """Receive an outbound op result (T9) → PROCESSING for next activation."""
        self.continuation.receive_external_result(result, tick)

    def __repr__(self) -> str:
        return (
            f"AgentRuntimeState({self.agent_id}, state={self.state.value}, "
            f"phase={self.continuation.phase.value})"
        )


_TASK_PRIORITY_RANK = {
    TaskPriority.LOW: 0,
    TaskPriority.NORMAL: 1,
    TaskPriority.HIGH: 2,
    TaskPriority.URGENT: 3,
}

# N1b（§5.1）：默认初始布线的**基础工具**——旧按 role 白名单三集合
# （root/manager/worker）的交集（read/write/ls），对所有
# agent 的自身 position 授予（直派形态）。业务工具（send_email /
# delegate / 集成工具等）经 config 声明的初始授予集或 register_tool /
# register_integration 的迟到注册补授获得；N3 组织架构/场景包可整体
# 替换本初始授予集。
_BASE_GRANT_TOOLS = frozenset({"read", "write", "ls"})


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

        # Initialize core subsystems — NOTE: TransactionBuffer and
        # AuditLog are created first so they can be injected into
        # device constructors (N1c-2 tool handler wiring).
        self._journal = TickJournal()
        self._audit_log = AuditLog(journal=self._journal)
        self._file_ops_audit = FileOpsAuditLog()
        # Transaction buffer for staged-effect commit (created early
        # for injection into device handlers via N1c-2 wiring).
        self._transaction_buffer = TransactionBuffer()

        # T16c: LLM usage budget accounting (per agent/task/simulation).
        # Persisted with the rest of the state — restart keeps the
        # accumulated counts (模拟重启不丢累计).
        self._budget = BudgetTracker(config=self._config.budget)
        self._private_store = PrivateStore(
            PrivateStoreConfig(
                base_path="private",
                max_storage_bytes=self._config.private_storage_limit_mb * 1024 * 1024,
            )
        )

        # Shared KB with permissions
        self._permission_engine = PermissionEngine()
        self._lock_manager = LockManager(
            default_lease_ticks=self._config.default_lock_lease_ticks,
        )
        # N1c-2: inject TransactionBuffer, AuditLog, and lock-tick
        # registration callback into SharedKB so its handler factories
        # have the kernel services they need.
        self._shared_kb = SharedKB(
            permissions=self._permission_engine,
            lock_manager=self._lock_manager,
            transaction_buffer=self._transaction_buffer,
            audit_log=self._audit_log,
            on_lock_acquired=self._register_tick_lock,
        )

        # T10: RecordStore (typed records) & AssetStore
        # (content-addressed binaries) — in-memory; SQLite persistence
        # is future work.
        # N1c-2: inject TransactionBuffer into RecordStore for handler factories.
        self._record_store = RecordStore(transaction_buffer=self._transaction_buffer)
        self._asset_store = AssetStore()

        # N1c-2: inject TransactionBuffer into MailSystem and TaskTree.
        self._mail_system = MailSystem(transaction_buffer=self._transaction_buffer)
        self._task_tree = TaskTree(transaction_buffer=self._transaction_buffer)

        # N1b（§5.1）：Authority（注册中心 + 布线中心，每 Team 仅一个）。
        # 一人公司 = 一实例一 Team（new_team_id）；Owner = 树根 agent。
        # 引导 = org 初始化 + 初始授予集（_initialize 布线，N3 可替换）。
        try:
            _owner = self._agent_tree.root_id
        except Exception:
            _owner = "agent.root"
        self._authority = Authority(
            team_id=new_team_id(),
            owner_agent_id=_owner,
        )

        # Tool registry (N1b: attached to the Authority — authorization
        # goes through two-layer Grants, §3.5/§5.1)
        # N4-4：相位提供器——CONSOLIDATING 会话期间（resume_phase 置位）
        # 授权集动态收窄为记忆工具集（工具面收窄，SPEC §4.4）。会话标记
        # 而非单相位值：会话跨越 CONSOLIDATING / WAITING_* /
        # PROCESSING_RESULT / READY_TO_DECIDE 等相位，收窄须覆盖整个
        # 会话（_agent_runtime_states 在 _initialize 填充，lambda 惰性求值）。
        self._tool_registry = ToolRegistry(
            authority=self._authority,
            phase_provider=lambda agent_id: (
                (
                    ContinuationPhase.CONSOLIDATING
                    if self._agent_runtime_states[agent_id].continuation.resume_phase
                    is not None
                    else self._agent_runtime_states[agent_id].continuation.phase
                )
                if agent_id in self._agent_runtime_states
                else None
            ),
        )

        # Pending operation registry (v0.6.0 — async LLM/tool tracking)
        self._pending_ops = PendingOperationRegistry()
        self._executors = ExecutorRegistry()
        # T9: Integration registry (external platform adapters) + provider
        # rate-limiting, kept separate from executor admission (决策1b).
        self._integrations = IntegrationRegistry()
        # T12b: CredentialStore (SPEC §7.5) — reference-only credential
        # resolution. The kernel holds only credential_ref strings; secret
        # VALUES are resolved at the executor/plugin boundary via
        # resolve(), never recorded. has() is the value-free admission
        # gate used at dispatch. Host installs real backends (env /
        # encrypted file) via set_credential_store().
        self._credential_store = CredentialStore()
        self._ingress = IngressBuffer()
        # Live subprocesses of in-process-executed ops (v0.8.0 P2-10):
        # request_id → Popen. cancel_operation kills the process group
        # for a physical cancel; dispatch registers/unregisters via
        # on_start/on_end.
        self._active_processes: dict[str, Any] = {}
        # Snapshot of the current tick (set at Freeze) — dispatch runs
        # in Publish and needs the frozen view for tool execution.
        self._last_snapshot: dict[str, Any] | None = None
        # This-tick transaction tracking for rollback (P0-2):
        # pending ops registered THIS tick that must be undone on rollback.
        self._tick_pending_ops: list[tuple[str, Any]] = []
        # Continuation snapshots BEFORE this-tick mutations, keyed by
        # agent_id → (phase, pending_request_id, pending_request_type).
        self._tick_continuations: dict[str, tuple[Any, str, str]] = {}
        # Locks acquired by write handlers during THIS tick's Act phase
        # (T20 写即自动锁): (resource, agent_id, lock_token). Released at
        # commit end — the lock never survives the tick; the lease is a
        # pure backstop.
        self._tick_acquired_locks: list[tuple[str, str, str]] = []

        # State epoch — incremented on rollback/restore. External results
        # carry the epoch they were submitted under; results from an older
        # epoch are stale and discarded (fencing).
        self._state_epoch = 0

        # Phase order of the last tick (kernel protocol observability)
        self._last_tick_phases: list[str] = []
        # Set by _phase_commit when the tick's effects were rolled back.
        # run_tick uses it to defer/requeue claimed wake events so the
        # rolled-back tick's activations re-trigger next tick.
        self._last_tick_rolled_back = False
        self._last_tick_rollback_error: str | None = None

        # Email outbox (reliable dispatch with idempotency)
        self._outbox = Outbox(max_retries=self._config.max_retries)

        # T19 crash guard: repeated kernel-level crashes → emergency
        # callbacks + auto-pause (reason=crash_guard, no auto-resume).
        self._crash_guard = CrashGuard(
            window_ticks=self._config.crash_guard_window_ticks,
            threshold=self._config.crash_guard_threshold,
            audit_log=self._audit_log,
            pause_action=self._pause_for_crash_guard,
        )
        # Why the simulation is paused ("" = not paused / manual).
        self._pause_reason: str = ""

        # Tick engine
        self._tick_engine = TickEngine(
            TickConfig(
                tick_duration_value=self._config.tick_duration_value,
                tick_duration_unit=self._config.tick_duration_unit,
                simulation_time_per_tick_value=self._config.simulation_time_per_tick_value,
                simulation_time_per_tick_unit=self._config.simulation_time_per_tick_unit,
                start_paused=self._config.start_paused,
                deterministic_mode=self._config.deterministic_mode,
            )
        )

        # Human control (pause/resume/view; tick duration apply is NOT
        # wired — see dead-module-cleanup TODO)
        self._human_control = HumanControl(
            tick_engine=self._tick_engine,
            agent_tree=self._agent_tree,
            task_tree=self._task_tree,
            mail_system=self._mail_system,
            shared_kb=self._shared_kb,
            audit_log=self._audit_log,
            ingress=self._ingress,  # T12a: human UI actions ingress
        )

        # Timeout checker (between Phase 8 Commit and Phase 9 Publish)
        self._timeout_checker = TimeoutChecker(
            task_tree=self._task_tree,
            lock_manager=self._lock_manager,
            audit_log=self._audit_log,
        )

        # N4-3 记忆子系统（每 agent 一实例；注入组装器消费）
        self._agent_memories: dict[str, AgentMemory] = {
            cfg.agent_id: AgentMemory(cfg.agent_id) for cfg in self._agent_tree
        }
        self._recall_configs: dict[str, RecallConfig] = {
            cfg.agent_id: RecallConfig() for cfg in self._agent_tree
        }
        self._recall_engines: dict[str, RecallEngine] = {
            cfg.agent_id: RecallEngine() for cfg in self._agent_tree
        }

        # N4-4 整理模式：CONSOLIDATING 进出判定（hysteresis 进 90%/出 80%）
        self._consolidation_gate = ConsolidationGate()

        # T6: ContextCompiler for role-aware observation assembly
        self._context_compiler = ContextCompiler(
            agent_tree=self._agent_tree,
            task_tree=self._task_tree,
            shared_kb=self._shared_kb,
            mail_system=self._mail_system,
            private_store=self._private_store,
            authority=self._authority,
            agent_memories=self._agent_memories,
            recall_configs=self._recall_configs,
            recall_engines=self._recall_engines,
            audit_log=self._audit_log,
        )

        # Agent scheduler (event-driven activation)
        self._scheduler = AgentScheduler(config=self._config.execution)
        # T11: calendar rules (SPEC §9.1) — advancement is a staged
        # RULE_ADVANCE effect, committed atomically with the rule's
        # task creation (T11 决策 1).
        self._calendar_store = CalendarStore()
        # Fires staged this tick; wakes enqueued post-commit only.
        self._calendar_fires_this_tick: list[dict[str, Any]] = []
        # T11 决策 3: round-robin cursors per pool manager. In-memory
        # fairness state — resets on restart (benign: fairness only;
        # least_busy is the default strategy and is stateless).
        self._pool_cursors: dict[str, int] = {}
        # T11: fire-once tracking for deadline wake events (task_id →
        # {"approaching", "expired"}). Rolled-back ticks un-mark what
        # they fired so events re-fire after rollback (no loss).
        self._deadline_fired: dict[str, set[str]] = {}
        self._deadline_fired_this_tick: list[tuple[str, str]] = []

        # T12a: pending human UI actions per kind=human agent. Ingressed
        # human-action events are routed here (assignee → [action dicts]);
        # the Observe phase injects them into the human worker's
        # observation and HumanWorkerRuntime translates them to Intents
        # through the normal transaction path. Consumed actions are
        # recorded so a rolled-back tick restores them (no lost action).
        self._pending_human_actions: dict[str, list[dict[str, Any]]] = {}
        self._human_actions_consumed_this_tick: list[tuple[str, dict[str, Any]]] = []

        # Agent runtimes
        self._runtimes: dict[str, AgentRuntime] = {}

        # Agent runtime states (authoritative state source)
        self._agent_runtime_states: dict[str, AgentRuntimeState] = {}

        # Initialize
        self._register_tool_handlers()
        self._initialize()

    def _initialize(self) -> None:
        """Set up all agents: mailboxes, private spaces, runtimes, scheduler.

        N1b（§5.1）引导布线：每个 agent 以自身为 position（直派形态，
        grant_membership(agent_id, agent_id)），基础工具（read/write/ls，
        旧按 role 白名单交集）+ config 声明的工具授予该 position（初始授予集，deny-by-
        default：未注册 uuid 的工具不授予）。授权求值本身一律走
        Authority（§3.5），本处只建初始授予集。
        """
        for agent_config in self._agent_tree:
            agent_id = agent_config.agent_id

            # Register mailbox
            self._mail_system.register_agent(agent_id)

            # Create private workspace
            self._private_store.initialize_agent(agent_id)

            # N1b 初始授予集 = 基础工具 ∪ config 声明的工具（白名单载体
            # 已废除，config.tools 仅作初始授予集来源；N3 场景包可替换）。
            # N4-4：记忆工具集并入初始授予集（记忆归属 agent 自身；
            # CONSOLIDATING 下授权集收窄为记忆工具集——工具面收窄只在
            # 相位门内收紧，不做相位外额外放宽）。
            declared = frozenset(agent_config.tools or [])
            grant_tools = _BASE_GRANT_TOOLS | declared | MEMORY_TOOL_NAMES
            self._tool_registry.declare_tools(agent_id, grant_tools)

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
            wake_types.update(
                {
                    WakeEventType.NEW_EMAIL,
                    WakeEventType.HUMAN_MESSAGE,
                    WakeEventType.TOOL_RESULT,
                    WakeEventType.CHILD_TASK_CHANGE,
                    WakeEventType.DEADLINE_APPROACHING,
                }
            )
            # T12a: kind=human workers wake on their own UI actions
            # (accept/complete/fail) — routed by _consume_ingress.
            if agent_config.kind == "human":
                wake_types.add(WakeEventType.HUMAN_ACTION)
            initial_condition = WakeCondition(
                event_types=wake_types,
                wake_at_tick=0,
            )
            self._scheduler.register_agent(agent_id, initial_condition)

            # Audit
            self._audit_log.record(
                AuditEventType.AGENT_CREATED,
                agent_id=agent_id,
                details={"role": agent_config.role, "tools": list(grant_tools)},
            )

    @staticmethod
    def _validate_write_path(path: str) -> str | None:
        """Reject unsafe file-write paths before staging.

        Returns an error string if the path is invalid, None if ok.
        Checks: empty path, absolute path, ``..`` segment traversal.
        Symlink escapes and deeper containment are caught later by
        ``PrivateStore.resolve_path`` at commit time.
        """
        if not path:
            return "write path must not be empty"
        if path.startswith("/"):
            return f"absolute path rejected: {path}"
        from os.path import normpath

        parts = normpath(path).split("/")
        if ".." in parts:
            return f"path traversal rejected: {path}"
        return None

    def _register_tick_lock(
        self,
        resource: str,
        agent_id: str,
        lock_token: str,
    ) -> None:
        """Register a lock acquired during this tick's Act phase.

        Called by SharedKB.make_handle_kb_write's handler via the
        on_lock_acquired callback (N1c-2 injection).  The tuple is
        consumed by _phase_commit (Phase 8) when releasing locks.
        """
        self._tick_acquired_locks.append((resource, agent_id, lock_token))
        return None

    def _staged_private_effects(self, agent_id: str) -> dict[str, str]:
        """Merge this agent's uncommitted staged file writes/patches.

        Keyed by resource path → final content. The disk is always the
        last committed state (staged effects are applied only at Commit),
        so "committed state + own staged" reads are computed on demand —
        no full-content snapshot is ever built (SPEC §3.1 冻结视图按需化).
        """
        merged: dict[str, str] = {}
        for e in self._transaction_buffer.get_effects(agent_id):
            if e.status not in (EffectStatus.STAGED, EffectStatus.VALIDATED):
                continue
            if e.effect_type in (EffectType.FILE_WRITE, EffectType.FILE_PATCH):
                merged[e.resource] = e.data.get("content", "")
            elif e.effect_type == EffectType.FILE_DELETE:
                merged.pop(e.resource, None)
        return merged

    def _read_private_file(
        self,
        agent_id: str,
        path: str,
    ) -> tuple[bool, str]:
        """Read a private file as the agent sees it: committed state
        (disk) overlaid with this agent's own staged writes.

        Returns (found, content). Forbidden paths raise AccessDeniedError
        via PrivateStore.resolve_path.
        """
        staged = self._staged_private_effects(agent_id)
        if path in staged:
            return True, staged[path]
        target = self._private_store.resolve_path(agent_id, path)
        if not target.exists() or not target.is_file():
            return False, ""
        return True, target.read_text(encoding="utf-8")

    def _read_private_file_bytes(
        self,
        agent_id: str,
        path: str,
    ) -> tuple[bool, bytes]:
        """Read a private file's raw BYTES (committed state on disk).

        T10: the private-file snapshot/read path no longer skips binary
        files — a READ-only caller can fetch the raw payload. The
        agent's own staged BINARY writes are folded in base64.
        """
        staged = self._staged_private_effects(agent_id)
        if path in staged:
            return True, self._staged_binary_content(agent_id, path)
        target = self._private_store.resolve_path(agent_id, path)
        if not target.exists() or not target.is_file():
            return False, b""
        return True, target.read_bytes()

    def _staged_binary_content(self, agent_id: str, path: str) -> bytes:
        """Raw bytes of a staged (uncommitted) binary file write."""
        effects = self._transaction_buffer.get_effects(agent_id)
        for e in effects:
            if e.effect_type != EffectType.FILE_WRITE:
                continue
            if e.resource != path:
                continue
            if e.effect_type is EffectType.FILE_WRITE and e.data.get(
                "is_binary",
                False,
            ):
                return base64.b64decode(e.data.get("content_bytes_b64", ""))
        return b""

    def _register_tool_handlers(self) -> None:
        """Register tool handlers that connect ToolRegistry to subsystems.

        N1c-2: handlers are now generated by device method factories
        (SharedKB / RecordStore / MailSystem / TaskTree) and agent_tools
        factories (private-workspace file tools + executor tools).
        simulation._register_tool_handlers is reduced to a thin wiring
        layer: it calls each factory, collects the handler callables, and
        registers them with the tool registry.

        Write tools (write, send_email, delegate) stage effects in the
        TransactionBuffer. grep tools (read, ls) execute directly.
        Kernel services (TransactionBuffer / AuditLog / LockManager etc.)
        were injected into devices at construction time.
        """
        from my_team.agent_tools import (
            make_handle_apply_patch,
            make_handle_git_diff,
            make_handle_git_status,
            make_handle_ls,
            make_handle_python_compute,
            make_handle_python_transform,
            make_handle_read,
            make_handle_run_tests,
            make_handle_write,
        )
        from my_team.memory_tools import (
            make_handle_memory_edit,
            make_handle_memory_evict,
            make_handle_memory_fold,
            make_handle_memory_pin,
            make_handle_memory_promote,
            make_handle_memory_retag,
        )

        manifests = builtin_manifests()
        handlers = {
            # §4.5 Private-workspace file tools (agent_tools.py)
            "read": make_handle_read(
                private_store=self._private_store,
                staged_private_effects=self._staged_private_effects,
                read_private_file=self._read_private_file,
            ),
            "ls": make_handle_ls(
                private_store=self._private_store,
                staged_private_effects=self._staged_private_effects,
            ),
            "write": make_handle_write(
                transaction_buffer=self._transaction_buffer,
            ),
            "apply_patch": make_handle_apply_patch(
                transaction_buffer=self._transaction_buffer,
                read_private_file=self._read_private_file,
            ),
            # §3.4 Executor/workspace tools (agent_tools.py)
            "run_tests": make_handle_run_tests(
                tool_registry=self._tool_registry,
                audit_log=self._audit_log,
                active_processes=self._active_processes,
            ),
            "python_compute": make_handle_python_compute(
                tool_registry=self._tool_registry,
                active_processes=self._active_processes,
            ),
            "python_transform": make_handle_python_transform(
                tool_registry=self._tool_registry,
                active_processes=self._active_processes,
                read_private_file=self._read_private_file,
            ),
            "git_diff": make_handle_git_diff(
                tool_registry=self._tool_registry,
                audit_log=self._audit_log,
            ),
            "git_status": make_handle_git_status(
                tool_registry=self._tool_registry,
                audit_log=self._audit_log,
            ),
            # §5.2 SharedKB device tools
            "kb_write": self._shared_kb.make_handle_kb_write(),
            "kb_read": self._shared_kb.make_handle_kb_read(),
            "kb_list": self._shared_kb.make_handle_kb_list(),
            "kb_search": self._shared_kb.make_handle_kb_search(),
            # §5.3 RecordStore device tools
            "record_upsert": self._record_store.make_handle_record_upsert(),
            "record_delta": self._record_store.make_handle_record_delta(),
            # §5.6 MailSystem device tools
            "send_email": self._mail_system.make_handle_send_email(),
            # §5.7 TaskTree tool (N1c-2: handler moved here; device
            # subclassing deferred to N1c-4)
            "delegate": self._task_tree.make_handle_delegate(),
            # N4-4 记忆工具集（CONSOLIDATING 工具面收窄目标；归属 Agent
            # 引擎数据面，经 memory_tools 工厂接线）
            "memory_fold": make_handle_memory_fold(
                agent_memories=self._agent_memories,
                recall_engines=self._recall_engines,
                transaction_buffer=self._transaction_buffer,
            ),
            "memory_promote": make_handle_memory_promote(
                agent_memories=self._agent_memories,
                recall_engines=self._recall_engines,
                transaction_buffer=self._transaction_buffer,
            ),
            "memory_edit": make_handle_memory_edit(
                agent_memories=self._agent_memories,
                recall_engines=self._recall_engines,
                transaction_buffer=self._transaction_buffer,
            ),
            "memory_retag": make_handle_memory_retag(
                agent_memories=self._agent_memories,
                recall_engines=self._recall_engines,
                transaction_buffer=self._transaction_buffer,
            ),
            "memory_evict": make_handle_memory_evict(
                agent_memories=self._agent_memories,
                recall_engines=self._recall_engines,
                transaction_buffer=self._transaction_buffer,
            ),
            "memory_pin": make_handle_memory_pin(
                agent_memories=self._agent_memories,
                recall_engines=self._recall_engines,
                recall_configs=self._recall_configs,
                transaction_buffer=self._transaction_buffer,
            ),
        }
        for name, handler in handlers.items():
            self.register_tool(manifests[name], handler)

        # Executor registration (v0.8.0 P1-4/5): LOCAL_PROCESS tools are
        # dispatched to a TRUSTED_IN_PROCESS executor (host subprocess
        # with timeout/truncation — sandbox_tools). Remote tools are
        # admitted by UNTRUSTED_OUT_OF_PROCESS executors registered by
        # the harness (tests/tool_helpers.py). T16a: run_tests is now
        # SANDBOXED_PROCESS — its kernel handler executes the tool in a
        # sandboxed OS process, so its executor tier is
        # SANDBOXED_OUT_OF_PROCESS (the only tier compatible with the
        # class; the dispatch loop still runs the handler in-process).
        for tool in ("python_compute", "python_transform"):
            self._executors.register(
                tool,
                tier=ExecutorTier.TRUSTED_IN_PROCESS,
                max_concurrent=2,
            )
        self._executors.register(
            "run_tests",
            tier=ExecutorTier.SANDBOXED_OUT_OF_PROCESS,
            max_concurrent=2,
        )

    def _create_runtime(self, config: AgentConfig) -> AgentRuntime:
        """Create an appropriate runtime for an agent based on config."""
        # T12a: kind=human agents are UI-queue driven — dedicated
        # runtime that only translates human UI actions to Intents.
        if config.kind == "human":
            return HumanWorkerRuntime(
                agent_id=config.agent_id,
                tool_registry=self._tool_registry,
            )
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
    def record_store(self) -> RecordStore:
        """T10: typed record store (schema-registered + ledger)."""
        return self._record_store

    @property
    def asset_store(self) -> AssetStore:
        """T10: content-addressed binary asset store."""
        return self._asset_store

    @property
    def tick_engine(self) -> TickEngine:
        return self._tick_engine

    @property
    def human_control(self) -> HumanControl:
        return self._human_control

    @property
    def scheduler(self) -> AgentScheduler:
        return self._scheduler

    @property
    def audit_log(self) -> AuditLog:
        return self._audit_log

    @property
    def budget(self) -> BudgetTracker:
        """LLM usage budget tracker (T16c)."""
        return self._budget

    @property
    def pending_ops(self) -> PendingOperationRegistry:
        """Public accessor for the pending operation registry (N4-6)."""
        return self._pending_ops

    # -- Tool plugin API (v0.10 T7) -----------------------------------------

    def register_tool(
        self,
        manifest: ToolManifest,
        handler: Any,
        executor: ExecutorTier | None = None,
        policy: OperationPolicy | None = None,
    ) -> None:
        """Public plugin API: register a tool WITHOUT touching kernel code.

        - ``manifest``: ToolManifest — validated at registration
          (raises ToolManifestError on invalid contract or duplicate name).
        - ``handler``: callable ``(context: ToolContext, **args)``.
          Plugin handlers access subsystems ONLY through the injected
          ``context.handles`` mapping (file / KB / mail / task tree /
          ...); they must never reach Simulation internals.
        - ``executor``: ExecutorTier — register an executor binding when
          the tool needs dispatch (LOCAL_PROCESS / SANDBOXED_PROCESS /
          EXTERNAL_IRREVERSIBLE). PURE / READ_ONLY / STAGED_MUTATION
          tools are kernel-executed and need none.
        - ``policy``: OperationPolicy — if given, attached as the
          deployment policy (deny-by-default: only allowlisted tools
          are usable). If None, this tool is NOT implicitly allowlisted;
          while a policy is active it stays denied until listed.

        N1b（§5.1）：注册时把工具 capability 作为受控 uuid 注册进
        Authority（注册中心），并对声明过该工具的 agent 补授（迟到注册
        补授，见 ``ToolRegistry._register_entity``）——注册本身不判权，
        授权判定由 Authority 两层 Grant 求值（§3.5）。
        """
        wrapped = self._wrap_plugin_handler(handler)
        self._tool_registry.register_tool(manifest, wrapped)
        if executor is not None:
            self._executors.register(manifest.name, tier=executor)
        if policy is not None:
            self._tool_registry.set_policy(policy)

    def _plugin_handles(self) -> MappingProxyType[str, Any]:
        """Read-only subsystem handles injected into plugin contexts.

        The injected set is the plugin's entire reachable surface: no
        Simulation internals, no unlisted subsystem. Handles are a
        MappingProxyType so plugins cannot smuggle in mutable aliases
        from the kernel side.
        """
        return MappingProxyType(
            {
                "private_store": self._private_store,
                "shared_kb": self._shared_kb,
                "record_store": self._record_store,  # T10
                "asset_store": self._asset_store,  # T10
                "mail_system": self._mail_system,
                "task_tree": self._task_tree,
                "agent_tree": self._agent_tree,
                "scheduler": self._scheduler,
                "outbox": self._outbox,
                "human_control": self._human_control,
                "audit_log": self._audit_log,
                "integrations": self._integrations,  # T9
                "ingress": self._ingress,  # T9
                "credential_store": self._credential_store,  # T12b §7.5
            }
        )

    def _wrap_plugin_handler(self, handler: Any) -> Any:
        """Inject subsystem handles into the ToolContext of a handler."""
        handles = self._plugin_handles()

        def wrapped(context: ToolContext, **kwargs: Any) -> Any:
            ctx = replace(context, handles=handles)
            return handler(context=ctx, **kwargs)

        return wrapped

    @property
    def current_tick(self) -> int:
        return self._tick_engine.current_tick

    @property
    def state_epoch(self) -> int:
        """State epoch — incremented on rollback/restore.

        External operations are stamped with the epoch at submission;
        results whose epoch does not match the current one are stale
        and discarded by Ingest (fencing).
        """
        return self._state_epoch

    # -- T9: integrations & ingress -----------------------------------------

    @property
    def integrations(self) -> IntegrationRegistry:
        """The kernel's integration (external platform adapter) registry."""
        return self._integrations

    @property
    def credential_store(self) -> CredentialStore:
        """Reference-only credential resolution service (SPEC §7.5).

        The kernel sees only credential_ref strings; resolve() belongs
        to the executor/plugin boundary performing the outbound call.
        """
        return self._credential_store

    def set_credential_store(self, store: CredentialStore) -> None:
        """Install the host's CredentialStore (replaces the empty default).

        Backends (env / encrypted file) are host-side configuration;
        the kernel only ever calls has()/resolve() on the store.
        """
        self._credential_store = store

    @property
    def ingress(self) -> IngressBuffer:
        """The kernel's inbound platform-event buffer (SPEC §8.1)."""
        return self._ingress

    def register_integration(self, integration: Integration) -> None:
        """Register an Integration (T9 一等公民).

        Dynamic outbound-tool registration (决策2): each manifest in
        ``integration.manifests`` is dispatched to the ExecutorRegistry as
        an UNTRUSTED_OUT_OF_PROCESS executor (the external platform is the
        out-of-process executor), so outbound tools go through the normal
        executor admission + dispatch path.
        """
        self._integrations.register(integration)
        for manifest in integration.manifests:
            # EXTERNAL_IRREVERSIBLE maps to UNTRUSTED_OUT_OF_PROCESS tier;
            # the platform (via its fake/scenario adapter) is the executor
            # and completes the op out-of-band.
            self._executors.register(
                manifest.name,
                tier=ExecutorTier.UNTRUSTED_OUT_OF_PROCESS,
                executor_id=f"provider.{integration.name}.{manifest.name}",
                max_concurrent=integration.rate_limits.max_calls,
            )
            self._tool_registry.register_manifest(manifest)

    def inject_ingress(self, event: IngressEvent) -> bool:
        """Accept an inbound platform event (deduplicates).

        Test/harness entry point for receiving platform events between
        ticks. Returns True if newly accepted (not a duplicate).
        """
        return self._ingress.receive(event)

    @property
    def last_tick_phases(self) -> list[str]:
        """Phase names executed in the most recent tick, in order."""
        return list(self._last_tick_phases)

    def _bump_state_epoch(self) -> None:
        """Invalidate all in-flight results from the previous epoch."""
        self._state_epoch += 1

    @property
    def is_paused(self) -> bool:
        """Check if the simulation is paused."""
        return self._tick_engine.state.value == "paused"

    @property
    def pause_reason(self) -> str:
        """Why the simulation is paused ('' = not paused / no reason)."""
        return self._pause_reason

    @property
    def crash_guard(self) -> CrashGuard:
        """T19 crash guard (crash detection + auto-pause + callbacks)."""
        return self._crash_guard

    def _pause_for_crash_guard(self, report: CrashReport) -> None:
        """Pause action wired into CrashGuard (T19): auto-pause with
        reason=crash_guard; never auto-resumes — a human must resume."""
        self.pause(reason="crash_guard")

    def pause(self, reason: str = "") -> None:
        """Pause the simulation — takes effect at the next commit boundary.

        Per SPEC §8.6: no new agent activations are scheduled, no state
        transitions are committed. Already-issued external requests
        continue; their results enter quarantine (they are collected by
        the Ingest phase only after resume).
        """
        self._pause_reason = reason
        self._tick_engine.pause()

    def resume(self) -> None:
        """Resume the simulation from a paused state.

        Re-arms the crash guard (T19): after a human resume the sliding
        window keeps aging; a renewed crash loop re-triggers it.
        """
        self._pause_reason = ""
        self._crash_guard.rearm()
        self._tick_engine.resume()

    def cancel_operation(
        self,
        request_id: str,
        agent_id: str | None = None,
    ) -> CancellationResult:
        """Cancel an in-flight operation (v0.7.0 P1-4).

        Rules:
        - Only SUBMITTED/PENDING ops can be cancelled (registry-level)
        - TOOL_REQUEST ops require the tool's manifest to declare
          supports_cancel; LLM requests are always logically cancellable
        - The agent is woken with a structured {error, cancelled,
          request_id} notice (mirrors the timeout path), NEVER with the
          op's result; a late result is fenced by complete()
        - Cancelled ops audit OP_CANCELLED and are removed from the
          registry

        Cancellation is LOGICAL: for remote tools the external harness
        is out of reach (executor_cancel_requested=False) and even an
        LLM request may already have provider-side effects (cost, logs,
        processing) that cancellation cannot undo
        (external_effects_possible=True). Returns a CancellationResult,
        never raising.
        """
        op = self._pending_ops.get_by_id(request_id)
        if op is None:
            return CancellationResult(
                accepted=False,
                request_id=request_id,
                reason="operation not found",
            )
        if agent_id is not None and op.agent_id != agent_id:
            return CancellationResult(
                accepted=False,
                request_id=request_id,
                op_type=op.op_type,
                reason=f"operation belongs to '{op.agent_id}', not '{agent_id}'",
            )
        if op.op_type == OpType.TOOL_REQUEST:
            tool_name = op.metadata.get("tool_name", "")
            manifest = self._tool_registry.get_manifest(tool_name)
            if manifest is None or not manifest.supports_cancel:
                return CancellationResult(
                    accepted=False,
                    request_id=request_id,
                    op_type=op.op_type,
                    reason=(f"tool '{tool_name}' does not declare supports_cancel"),
                )

        cancelled = self._pending_ops.cancel(request_id)
        if cancelled is None:
            return CancellationResult(
                accepted=False,
                request_id=request_id,
                op_type=op.op_type,
                reason="operation is no longer in flight (terminal/completed)",
            )

        # PHYSICAL cancel (v0.8.0 P2-10): if an in-process executor is
        # running a subprocess for this op (LOCAL_PROCESS tools like
        # python_compute / run_tests), kill the whole process group.
        # The dispatch loop is blocked in communicate(); the kill makes
        # it return, and the op is already CANCELLED — the late result
        # is fenced (complete_tool ignores terminal ops).
        executor_cancel_requested = False
        executor_cancel_confirmed = False
        proc = self._active_processes.get(request_id)
        if proc is not None:
            executor_cancel_requested = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                executor_cancel_confirmed = True
            except (ProcessLookupError, PermissionError):
                executor_cancel_confirmed = False

        self._audit_log.record(
            AuditEventType.OP_CANCELLED,
            agent_id=op.agent_id,
            tick=self._tick_engine.current_tick,
            details={
                "request_id": request_id,
                "op_type": op.op_type.value,
                "tool_name": op.metadata.get("tool_name", ""),
                "reason": (
                    "manifest supports_cancel" if op.op_type == OpType.TOOL_REQUEST else "system"
                ),
            },
            success=True,
            error=None,
        )

        # Wake the waiting agent with a structured cancellation notice
        # (the RESULT is never delivered — only the notice).
        runtime_state = self._agent_runtime_states.get(op.agent_id)
        if (
            runtime_state is not None
            and runtime_state.continuation.pending_request_id == request_id
        ):
            notice: dict[str, Any] = {
                "error": f"Operation cancelled (request {request_id})",
                "cancelled": True,
                "request_id": request_id,
            }
            if op.op_type == OpType.LLM_REQUEST:
                runtime_state.receive_llm_result(notice, self._tick_engine.current_tick)
            elif op.op_type == OpType.TOOL_REQUEST:
                runtime_state.receive_tool_result(notice, self._tick_engine.current_tick)
            self._enqueue_result_wake(op, self._tick_engine.current_tick, result=notice)

        self._pending_ops.remove(request_id)
        return CancellationResult(
            accepted=True,
            request_id=request_id,
            op_type=op.op_type,
            result_fenced=True,
            executor_cancel_requested=executor_cancel_requested,
            executor_cancel_confirmed=executor_cancel_confirmed,
            # The op may already have produced external side effects
            # (provider processing/cost/logs, files written before the
            # kill) that cannot be undone.
            external_effects_possible=True,
        )

    @classmethod
    def from_config_file(cls, path: str | Path) -> Simulation:
        """Create a simulation from a JSON config file."""
        config_path = Path(path)
        with open(config_path) as f:
            data = json.load(f)

        sim_config = SimulationConfig(**data.get("simulation", {}))
        agent_tree = AgentTree.from_dict(data)

        return cls(agent_tree=agent_tree, config=sim_config)

    # -- Persistence (P3-11: SQLite save/load) ------------------------------

    def save_to(self, path: str | Path) -> None:
        """Persist the full simulation state to a SQLite database.

        All components are written in ONE transaction: either the
        previous state remains or the new state is complete — never
        partial. Save at tick boundaries (the transaction buffer is
        empty after each tick). Private workspace files stay on disk
        under the saved base path; the DB captures everything else.
        """
        store = SimulationStore(path)
        store.save(self._collect_state())

    @classmethod
    def load_from(cls, path: str | Path) -> Simulation:
        """Reconstruct a simulation from a saved database (crash recovery).

        The simulation is rebuilt from the saved agent tree + config,
        then every subsystem's state is restored: tick engine, tasks,
        emails, scheduler events, outbox, pending ops, shared KB
        (resources + versions + permissions), locks, audit, agent
        runtime states (state machine + continuation), and the state
        epoch.

        Agent runtime LOGIC is not persisted: runtimes are rebuilt by
        role from the agent config (RootAgent/ManagerAgent/SubAgent).
        Callers that inject custom runtime classes (e.g. LLMAgent or
        test doubles) must re-install them after load, exactly like
        they do when constructing a fresh simulation.
        """
        store = SimulationStore(path)
        state = store.load()
        if state is None:
            raise FileNotFoundError(f"No saved simulation state at '{path}'")
        saved_version = store.schema_version()
        if saved_version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported persistence schema version: {saved_version} "
                f"(expected {SCHEMA_VERSION})"
            )

        config = SimulationConfig(**state["config"])
        agent_tree = AgentTree.from_dict({"agents": state["agent_tree"]})
        sim = cls(agent_tree=agent_tree, config=config)
        sim._restore_state(state)
        return sim

    def _collect_state(self) -> dict[str, Any]:
        """Serialize all subsystem state into JSON-safe component blobs."""
        return {
            "config": self._config.model_dump(mode="json"),
            "agent_tree": [c.model_dump(mode="json") for c in self._agent_tree],
            "tick_engine": {
                "current_tick": self._tick_engine.current_tick,
                "state": self._tick_engine.state.value,
            },
            "state_epoch": self._state_epoch,
            "pause_reason": self._pause_reason,
            "private_store_base_path": str(self._private_store._config.base_path),
            "tasks": {
                "tasks": {
                    tid: t.model_dump(mode="json") for tid, t in self._task_tree._tasks.items()
                },
                "parent_map": self._task_tree._parent_map,
                "children_map": self._task_tree._children_map,
                "assignee_map": self._task_tree._assignee_map,
            },
            "emails": {
                "all": {
                    eid: e.model_dump(mode="json")
                    for eid, e in self._mail_system._all_emails.items()
                },
                "pending": [e.email_id for e in self._mail_system._pending],
                "mailboxes": {
                    aid: {
                        "inbox": [e.email_id for e in mb._inbox.values()],
                        "outbox": [e.email_id for e in mb._outbox.values()],
                    }
                    for aid, mb in self._mail_system._mailboxes.items()
                },
            },
            "scheduler": {
                "wake_conditions": {
                    aid: c.model_dump(mode="json")
                    for aid, c in self._scheduler._wake_conditions.items()
                },
                "events": [qe.model_dump(mode="json") for qe in self._scheduler._events],
                "activation_history": [
                    a.model_dump(mode="json") for a in self._scheduler._activation_history
                ],
                "activation_counter": self._scheduler._activation_counter,
            },
            "outbox": {
                "entries": [e.model_dump(mode="json") for e in self._outbox._entries.values()],
                "max_retries": self._outbox._max_retries,
            },
            "pending_ops": {
                "operations": [
                    op.model_dump(mode="json") for op in self._pending_ops._operations.values()
                ],
                "seen_requests": self._pending_ops.seen_requests_snapshot(),
            },
            "ingress": snapshot_ingress_buffer(self._ingress),
            "kb": {
                "resources": [
                    r.model_dump(mode="json") for r in self._shared_kb._resources.values()
                ],
                "versions": [
                    v.model_dump(mode="json") for v in self._shared_kb.versions._versions.values()
                ],
                "permissions": [r.model_dump(mode="json") for r in self._permission_engine._rules],
            },
            "record_store": {
                "schemas": [
                    s.model_dump(mode="json") for s in self._record_store._schemas.values()
                ],
                "records": {k: dict(r) for k, r in self._record_store._records.items()},
                "ledger": [e.model_dump(mode="json") for e in self._record_store._ledger],
            },
            "asset_store": self._asset_store.snapshot(),
            "locks": {
                "locks": [
                    lock.model_dump(mode="json") for lock in self._lock_manager._locks.values()
                ],
                "lock_counter": self._lock_manager._lock_counter,
            },
            "audit": {
                "entries": [e.model_dump(mode="json") for e in self._audit_log._entries],
                "next_event_id": self._audit_log._counter,
            },
            "tick_journal": {
                "records": [r.model_dump(mode="json") for r in self._journal.records],
            },
            "file_ops_audit": [e.model_dump(mode="json") for e in self._file_ops_audit._entries],
            "agent_states": {
                aid: {
                    "state": rs.state_machine.state.value,
                    "transition_count": rs.state_machine.transition_count,
                    "continuation": rs.continuation.model_dump(mode="json"),
                    "active_activation_id": rs.active_activation_id,
                    "last_activation_tick": rs.last_activation_tick,
                }
                for aid, rs in self._agent_runtime_states.items()
            },
            # T12a: pending human UI actions — must survive crash between
            # Ingest (ingress drained, seen-key recorded) and Decide.
            "human_pending_actions": {
                aid: [dict(a) for a in actions]
                for aid, actions in self._pending_human_actions.items()
            },
            # T16c: LLM usage budget accumulators — restart keeps the
            # cumulative counts (模拟重启不丢累计), so budget rejections
            # stay consistent across restarts.
            "budget": self._budget.snapshot(),
        }

    def _restore_state(self, state: dict[str, Any]) -> None:
        """Restore subsystem state into a freshly constructed simulation.

        The constructor already built agents, mailboxes, runtimes and
        registered scheduler conditions; this overwrites them with the
        persisted state.
        """
        # Tick engine + state epoch
        te = state["tick_engine"]
        self._tick_engine._current_tick = te["current_tick"]
        self._tick_engine._state = SimulationState(te["state"])
        self._pause_reason = state.get("pause_reason", "")
        self._state_epoch = state["state_epoch"]

        # Private store (files live on disk under the saved base path)
        base = state.get("private_store_base_path", "private")
        self._private_store = PrivateStore(
            PrivateStoreConfig(
                base_path=base,
                max_storage_bytes=self._config.private_storage_limit_mb * 1024 * 1024,
            )
        )
        for agent_config in self._agent_tree:
            self._private_store.initialize_agent(agent_config.agent_id)

        # Tasks
        task_state = state["tasks"]
        self._task_tree._tasks = {
            tid: Task.model_validate(d) for tid, d in task_state["tasks"].items()
        }
        self._task_tree._parent_map = dict(task_state["parent_map"])
        self._task_tree._children_map = {k: list(v) for k, v in task_state["children_map"].items()}
        self._task_tree._assignee_map = {k: list(v) for k, v in task_state["assignee_map"].items()}

        # Emails
        email_state = state["emails"]
        all_emails = {eid: Email.model_validate(d) for eid, d in email_state["all"].items()}
        ms = self._mail_system
        ms._all_emails = all_emails
        ms._pending = [all_emails[eid] for eid in email_state["pending"]]
        for aid, mb_state in email_state["mailboxes"].items():
            mb = ms._mailboxes.get(aid)
            if mb is None:
                continue
            mb._inbox = {eid: all_emails[eid] for eid in mb_state["inbox"] if eid in all_emails}
            mb._outbox = {eid: all_emails[eid] for eid in mb_state["outbox"] if eid in all_emails}

        # Scheduler
        sched = state["scheduler"]
        self._scheduler._wake_conditions = {
            aid: WakeCondition.model_validate(d) for aid, d in sched["wake_conditions"].items()
        }
        self._scheduler._events = [QueuedEvent.model_validate(d) for d in sched["events"]]
        self._scheduler._activation_history = [
            AgentActivation.model_validate(d) for d in sched["activation_history"]
        ]
        self._scheduler._activation_counter = sched["activation_counter"]

        # Outbox
        ob = state["outbox"]
        self._outbox._entries = {
            e["entry_id"]: OutboxEntry.model_validate(e) for e in ob["entries"]
        }
        self._outbox._idempotency_keys = {e.idempotency_key for e in self._outbox._entries.values()}
        self._outbox._max_retries = ob["max_retries"]

        # Pending operations (+ request_id history for replay dedupe)
        self._pending_ops._operations = {
            op["request_id"]: PendingOperation.model_validate(op)
            for op in state["pending_ops"]["operations"]
        }
        self._pending_ops.restore_seen_requests(
            state["pending_ops"].get("seen_requests", {}),
        )

        # T9: IngressBuffer — cross-restart dedup of (source, external_id)
        self._ingress = restore_ingress_buffer(state.get("ingress"))

        # T12a: pending human UI actions (survive crash between Ingest
        # drain and Decide translation).
        self._pending_human_actions = {
            aid: [dict(a) for a in actions]
            for aid, actions in state.get("human_pending_actions", {}).items()
        }
        self._human_actions_consumed_this_tick = []

        # T16c: budget accumulators — restore cumulative usage so the
        # budget judgment stays consistent across a restart. Saves from
        # before this feature have no "budget" key → fresh accumulator.
        self._budget.restore(state.get("budget") or {})

        # Shared KB (resources + versions + permissions)
        kb = state["kb"]
        self._shared_kb._resources = {
            r["path"]: SharedKBResource.model_validate(r) for r in kb["resources"]
        }
        self._shared_kb.versions._versions = {
            v["path"]: VersionInfo.model_validate(v) for v in kb["versions"]
        }
        self._permission_engine._rules = [
            PermissionRule.model_validate(r) for r in kb["permissions"]
        ]

        # T10: RecordStore + AssetStore restoration
        rs = state.get("record_store")
        if rs:
            from my_team.record_store import LedgerEntry, RecordSchema

            self._record_store._schemas = {
                s["record_type"]: RecordSchema.model_validate(s) for s in rs.get("schemas", [])
            }
            self._record_store._records = {
                key: dict(rec) for key, rec in rs.get("records", {}).items()
            }
            self._record_store._ledger = [
                LedgerEntry.model_validate(e) for e in rs.get("ledger", [])
            ]
            self._record_store._ledger_counter = max(
                (e.ledger_id for e in self._record_store._ledger), default=0
            )
            self._record_store._version_counter = {
                key: len(
                    [e for e in self._record_store._ledger if f"{e.record_type}:{e.key}" == key]
                )
                for key in self._record_store._records
            }
        asset_state = state.get("asset_store")
        if asset_state:
            self._asset_store.restore(asset_state)

        # Locks
        locks = state["locks"]
        self._lock_manager._locks = {
            lock["resource"]: LockInfo.model_validate(lock) for lock in locks["locks"]
        }
        self._lock_manager._lock_counter = locks["lock_counter"]

        # T4: Tick Journal + Audit reconstruction
        journal_state = state.get("tick_journal", {})
        if journal_state.get("records"):
            from my_team.journal import TickRecord

            self._journal._records = [
                TickRecord.model_validate(r) for r in journal_state["records"]
            ]
        # Always restore audit from the direct blob (includes init events
        # that happen before any TickRecord).  The Journal is the
        # authoritative source for tick-scoped events; the blob is the
        # source for pre-tick events.  During execution, both are kept
        # in sync via AuditLog.record() → Journal delegation.
        audit = state["audit"]
        self._audit_log._entries = [AuditEntry.model_validate(e) for e in audit["entries"]]
        self._audit_log._counter = audit["next_event_id"]

        # File ops audit
        self._file_ops_audit._entries = [
            FileOpsAuditEntry.model_validate(e) for e in state.get("file_ops_audit", [])
        ]

        # Agent runtime states (state machine + continuation)
        for aid, rs_state in state["agent_states"].items():
            rs = self._agent_runtime_states.get(aid)
            if rs is None:
                continue
            rs.state_machine = AgentStateMachine(
                agent_id=aid,
                initial_state=AgentState(rs_state["state"]),
            )
            rs.state_machine._transition_count = rs_state["transition_count"]
            rs.continuation = AgentContinuation.model_validate(rs_state["continuation"])
            rs.active_activation_id = rs_state["active_activation_id"]
            rs.last_activation_tick = rs_state["last_activation_tick"]

    # -- Tick execution (10 phases) -----------------------------------------

    def run_tick(self) -> TickResult:
        """Execute one complete tick through the 10-phase kernel cycle.

        Guarded by the T19 crash guard: an UNCAUGHT exception here is a
        systemic defect — recorded as a crash event (repeated crashes →
        emergency callbacks + auto-pause). The exception is re-raised so
        callers see it; deterministic business failures (local FAILED)
        never surface through this path.
        """
        try:
            return self._run_tick_impl()
        except Exception as e:  # noqa: BLE001 — crash guard hooks every crash
            if not self.is_paused:
                self._crash_guard.record_crash(
                    self._tick_engine.current_tick,
                    str(e),
                    self._state_epoch,
                )
            raise

    def _run_tick_impl(self) -> TickResult:
        """Execute one complete tick through the 10-phase kernel cycle.

        Per SPEC §8.6: Tick is the kernel's state commit unit, NOT the
        agent's ReAct cycle. Each phase is finite and non-blocking.

        Responsibility split (per the v0.6.0 review, 方案 B):
          Validate — Pre-Validate(Intent): may the agent even attempt this?
          Act      — translate validated Intents into staged effects /
                     registered pending operations (no application)
          Commit   — Commit-Validate(Effect/PendingOp) then apply;
                     rollback + state-epoch bump on failure

        Phases:
         1. Ingest   — collect completed external events (LLM, tool, human),
                       fence stale/superseded results, wake timed-out agents,
                       deliver emails whose deliver_at_tick <= current_tick
         2. Freeze   — snapshot global state (incl. per-agent private file view)
         3. Schedule — compute ready set from events + agent states
         4. Resume   — restore agent continuation, read new events
         5. Decide   — generate Intents (non-blocking, no sync LLM/tool)
         6. Validate — pre-validate Intents (tool capability, delegation,
                       payload, LLM budget, duplicate request_id)
         7. Act      — translate Intents: stage effects, register pending ops
         8. Commit   — commit-validate + apply staged effects; rollback on failure
         9. Publish  — dispatch pending ops, generate wake events; timeouts
        10. Audit    — record all events
        """
        if self.is_paused:
            raise RuntimeError(
                "Cannot run a tick while paused. Call resume() first. "
                "External results are quarantined until resume."
            )

        # Apply pending tick duration changes at tick boundary
        self._human_control.apply_pending_duration_changes()

        tick = self._tick_engine.current_tick
        self._last_tick_phases = [
            "ingest",
            "freeze",
            "schedule",
            "observe",
            "decide",
            "validate",
            "act",
            "commit",
            "publish",
            "audit",
        ]

        # T4: start journal record for this tick
        self._journal.start_tick(tick, self._state_epoch)
        self._deadline_fired_this_tick = []
        self._calendar_fires_this_tick = []

        # Phase 1: Ingest — collect completed external operations + deliver emails
        self._phase_ingest(tick)
        delivered = self._phase_deliver(tick)

        # Phase 2: Freeze — snapshot global state
        snapshot = self._build_snapshot(tick)
        self._last_snapshot = snapshot

        # Phase 3: Schedule — determine which agents activate
        ready = self._phase_schedule(tick)

        # Phase 4: Resume — restore agent continuation, read new events
        observations = self._phase_observe(tick, snapshot, ready)

        # Phase 5: Decide — generate Intents (non-blocking)
        plans = self._phase_decide(tick, observations, ready)

        # T4: capture intents into journal
        self._capture_intents(plans)

        # Phase 6: Validate — pre-validate Intents before execution
        validated = self._phase_validate(tick, plans, ready)

        # T4: capture validation results into journal
        self._capture_validation(plans, validated)

        # Phase 7: Act — translate validated Intents into staged effects
        #            and registered pending operations (no application)
        all_results = self._phase_act(tick, plans, ready, validated, snapshot)
        # Phase 8: Commit — apply staged effects, register pending ops
        self._phase_commit(tick, all_results)
        self._transaction_buffer.clear()

        # Phase 9: Publish — dispatch pending ops, generate wake events
        self._phase_publish(tick, delivered, all_results, ready)

        # Phase 10: Audit
        self._phase_audit(tick, delivered, all_results, ready)

        # T4: finalize journal record for this tick
        current_rec = self._journal.current_record
        if current_rec is not None:
            from my_team.tool_protocol import hash_payload

            current_rec.snapshot_hash = hash_payload(self._last_snapshot or {})
            if self._last_tick_rolled_back:
                self._journal.finalize(
                    TickRecordStatus.ABORTED,
                    error=self._last_tick_rollback_error,
                )
            else:
                self._journal.finalize(TickRecordStatus.COMMITTED)

        # Complete activations and clean up scheduler. On COMMIT
        # ROLLBACK the tick's state was invalidated: activations
        # complete as FAILED (claims deferred) and their wake events are
        # requeued — the agents re-activate next tick and re-observe
        # the rolled-back state.
        rolled_back = self._last_tick_rolled_back
        if rolled_back:
            # T11: the tick's staged state was invalidated — un-mark
            # deadline fires so TIMER_EXPIRY / DEADLINE_APPROACHING
            # re-fire after re-execution (no lost wake).
            self._unmark_deadline_fires()
            # T12a: restore consumed human UI actions so they re-observe
            # and re-translate after re-execution (no lost action).
            for agent_id, act in self._human_actions_consumed_this_tick:
                self._pending_human_actions.setdefault(agent_id, []).append(act)
        self._human_actions_consumed_this_tick = []
        for candidate in ready:
            activation = self._scheduler._activations_this_tick.get(candidate.agent_id)
            if activation:
                self._scheduler.complete_activation(
                    activation.activation_id, success=not rolled_back
                )
                # Transition agent state: PROCESSING → IDLE
                runtime_state = self._agent_runtime_states.get(candidate.agent_id)
                if runtime_state:
                    runtime_state.complete_activation(tick)
        if rolled_back:
            for candidate in ready:
                activation = self._scheduler._activations_this_tick.get(
                    candidate.agent_id,
                )
                if activation:
                    self._scheduler.requeue_events([e.event_id for e in activation.wake_events])
        self._scheduler.end_tick()

        # Advance the clock — unless this tick paused the system
        # mid-flight (T19 crash guard, or a boundary pause): the 10
        # phases already completed; a paused clock must not advance
        # (the next tick will refuse to run until resume).
        if not self.is_paused:
            self._tick_engine.advance(1)

        # P0-3: construct real TickResult from the actual 10-phase cycle
        return TickResult(
            tick=tick,
            phases_completed=list(self._last_tick_phases),
            committed=not rolled_back,
            errors=[{"phase": "commit", "error": "tick rolled back"}] if rolled_back else [],
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

        For each operation whose result has arrived:
          1. Fence — discard stale-epoch results and results for
             superseded requests (agent no longer waiting on them)
          2. Deliver the result to the agent's continuation
          3. Publish a wake event for re-activation

        Timed-out operations are removed and the waiting agent is woken
        with a structured error (the agent decides retry / fail /
        escalate).
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
                    "state_epoch": op.state_epoch,
                },
                success=False,
                error=f"Operation timed out at tick {tick}",
            )

            # Wake the waiting agent with a structured timeout error.
            runtime_state = self._agent_runtime_states.get(op.agent_id)
            if (
                runtime_state is not None
                and runtime_state.continuation.pending_request_id == op.request_id
            ):
                error_result: dict[str, Any] = {
                    "error": f"Operation timed out at tick {tick}",
                    "timed_out": True,
                    "request_id": op.request_id,
                }
                if op.op_type == OpType.LLM_REQUEST:
                    runtime_state.receive_llm_result(error_result, tick)
                elif op.op_type == OpType.TOOL_REQUEST:
                    if op.metadata.get("external_tool"):
                        runtime_state.receive_external_result(error_result, tick)
                    else:
                        runtime_state.receive_tool_result(error_result, tick)
                self._enqueue_result_wake(op, tick, result=error_result)

            self._pending_ops.remove(op.request_id)

        # Failed operations (v0.8.0 P2-9 — executor crash / worker
        # death): wake the waiting agent with a structured error so it
        # can retry / fail / escalate. FAILED is terminal: the op is
        # removed after the wake.
        failed = [
            op for op in self._pending_ops._operations.values() if op.status == OpStatus.FAILED
        ]
        for op in failed:
            self._audit_log.record(
                AuditEventType.TOOL_RESULT,
                agent_id=op.agent_id,
                tick=tick,
                details={
                    "request_id": op.request_id,
                    "op_type": op.op_type.value,
                    "status": "failed",
                    "error": op.error,
                    "state_epoch": op.state_epoch,
                },
                success=False,
                error=op.error or "operation failed",
            )
            runtime_state = self._agent_runtime_states.get(op.agent_id)
            if (
                runtime_state is not None
                and runtime_state.continuation.pending_request_id == op.request_id
            ):
                failed_result: dict[str, Any] = {
                    "error": op.error or f"Operation failed ({op.request_id})",
                    "failed": True,
                    "request_id": op.request_id,
                }
                if op.op_type == OpType.LLM_REQUEST:
                    runtime_state.receive_llm_result(failed_result, tick)
                elif op.op_type == OpType.TOOL_REQUEST:
                    if op.metadata.get("external_tool"):
                        runtime_state.receive_external_result(failed_result, tick)
                    else:
                        runtime_state.receive_tool_result(failed_result, tick)
                self._enqueue_result_wake(op, tick, result=failed_result)
            self._pending_ops.remove(op.request_id)

        # Collect completed operations eligible for this tick
        completed = self._pending_ops.collect_completed(tick)
        for op in completed:
            runtime_state = self._agent_runtime_states.get(op.agent_id)

            # Fence 1: result from an older state epoch (post-rollback /
            # restore) is stale — the state it was computed against no
            # longer exists.
            if op.state_epoch != self._state_epoch:
                self._audit_log.record(
                    AuditEventType.STALE_RESULT,
                    agent_id=op.agent_id,
                    tick=tick,
                    details={
                        "request_id": op.request_id,
                        "op_type": op.op_type.value,
                        "reason": "epoch_mismatch",
                        "op_epoch": op.state_epoch,
                        "current_epoch": self._state_epoch,
                    },
                    success=False,
                    error="Result belongs to a superseded state epoch",
                )
                self._pending_ops.remove(op.request_id)
                continue

            # Fence 2: the agent is no longer waiting for this request
            # (it moved on, e.g. resubmitted after a timeout) — the
            # result belongs to a superseded operation.
            if (
                runtime_state is None
                or runtime_state.continuation.pending_request_id != op.request_id
            ):
                self._audit_log.record(
                    AuditEventType.STALE_RESULT,
                    agent_id=op.agent_id,
                    tick=tick,
                    details={
                        "request_id": op.request_id,
                        "op_type": op.op_type.value,
                        "reason": "superseded",
                        "pending_request_id": (
                            runtime_state.continuation.pending_request_id if runtime_state else ""
                        ),
                    },
                    success=False,
                    error="Result for superseded operation discarded",
                )
                self._pending_ops.remove(op.request_id)
                continue

            # Deliver the result to the agent's continuation
            if op.op_type == OpType.LLM_REQUEST:
                runtime_state.receive_llm_result(op.result, tick)
                # T16c: charge the completed invocation to the budget
                # tracker (agent/task/simulation accumulators).
                self._record_llm_usage(op, tick)
            elif op.op_type == OpType.TOOL_REQUEST:
                if op.metadata.get("external_tool"):
                    runtime_state.receive_external_result(op.result, tick)
                else:
                    runtime_state.receive_tool_result(op.result, tick)

            self._enqueue_result_wake(op, tick, result=op.result)

            # Audit the delivered tool result with the contract fields
            # (manifest_hash / tool_version / input_hash / output_hash
            # enter the replay context — v0.8.0 P1-3)
            if op.op_type == OpType.TOOL_REQUEST:
                tr = op.tool_request
                contract = op.metadata.get("tool_result")
                details: dict[str, Any] = {
                    "request_id": op.request_id,
                    "tool_name": (
                        tr.tool_name if tr is not None else op.metadata.get("tool_name", "")
                    ),
                    "state_epoch": op.state_epoch,
                }
                if tr is not None:
                    details["tool_version"] = tr.tool_version
                    details["manifest_hash"] = tr.manifest_hash
                    details["input_hash"] = tr.input_hash
                if isinstance(contract, dict):
                    details["output_hash"] = contract.get("output_hash", "")
                    details["result_status"] = contract.get("status", "")
                    details["executor_cancel_confirmed"] = contract.get(
                        "executor_cancel_confirmed",
                        False,
                    )
                self._audit_log.record(
                    AuditEventType.TOOL_RESULT,
                    agent_id=op.agent_id,
                    tick=tick,
                    details=details,
                )

            # Remove the consumed operation so it is not re-delivered
            self._pending_ops.remove(op.request_id)

        # Ingress: consume buffered inbound platform events (SPEC §8.1).
        # Each event is either a fresh external event (wakes related
        # agents via an EXTERNAL_RESULT advisory) or a receipt resolving
        # to an outbound op (external_id -> op_id via the Integration's
        # ReceiptAssertion, 决策4) which completes that op and wakes the
        # owning agent through the normal wait/wake path (决策3).
        self._consume_ingress(tick)

    def _consume_ingress(self, tick: int) -> None:
        """Drain the IngressBuffer and route events in the Ingest phase.

        - An event whose source matches an Integration with a ReceiptAssertion
          is attempted as a receipt: if op_id resolves and the op is still in
          flight, it is completed with the payload as result → wake the agent.
        - Otherwise it is a standalone external event: audit it and emit an
          EXTERNAL_RESULT advisory wake targeted at any agent subscribed to
          that event type (unknown → dropped; the mapping to a specific
          ProcessInstance is v0.11 E1).
        """
        for ev in self._ingress.drain():
            integration = self._integrations.get(ev.source)
            if integration is not None and integration.receipt is not None:
                ext_id = ev.payload.get(integration.receipt.external_id_field, "")
                if isinstance(ext_id, str) and ext_id:
                    op_id = self._integrations.resolve_op_id(
                        integration.name,
                        ext_id,
                        ev.payload,
                    )
                    if op_id is not None:
                        op = self._pending_ops.get_by_id(op_id)
                        if op is not None and op.status in {
                            OpStatus.SUBMITTED,
                            OpStatus.PENDING,
                        }:
                            op.result = {
                                "external_id": ext_id,
                                **(ev.payload.get("result") or ev.payload),
                            }
                            self._pending_ops.complete(op_id, result=op.result)
                            self._audit_log.record(
                                AuditEventType.TOOL_RESULT,
                                agent_id=op.agent_id,
                                tick=tick,
                                details={
                                    "request_id": op.request_id,
                                    "tool_name": ev.source,
                                    "status": "external_completed",
                                    "external_id": ext_id,
                                },
                            )
                            # Completion is delivered next loop iteration;
                            # also wake immediately via collect_completed
                            # path below.
                        continue
            # T12a: human UI action (accept/complete/fail) — route to the
            # task's assignee when it is a kind=human worker. The action
            # is parked in the pending queue; Observe injects it into the
            # worker's observation and HumanWorkerRuntime translates it
            # to an Intent through the normal transaction path (same
            # channel as AI workers — SPEC §10.1). Dedup key
            # (source, external_id) = ("human", task_id:action) makes a
            # repeated click idempotent. Unroutable human actions are
            # DROPPED (audited), never broadcast as advisory wakes.
            if ev.source == "human":
                action = ev.payload.get("action", "")
                task_id = ev.payload.get("task_id", "")
                if (
                    action in {"accept", "complete", "fail"}
                    and task_id
                    and self._task_tree.exists(task_id)
                ):
                    task = self._task_tree.get(task_id)
                    assignee = task.assignee_agent_id
                    cfg = self._agent_tree.get(assignee) if assignee in self._agent_tree else None
                    if cfg is not None and cfg.kind == "human":
                        self._pending_human_actions.setdefault(
                            assignee,
                            [],
                        ).append(
                            {
                                "action": action,
                                "task_id": task_id,
                                **{
                                    k: v
                                    for k, v in ev.payload.items()
                                    if k not in ("action", "task_id")
                                },
                            }
                        )
                        self._scheduler.enqueue_event(
                            WakeupEvent(
                                event_type=WakeEventType.HUMAN_ACTION,
                                target_agent_id=assignee,
                                tick=tick,
                                visible_at_tick=tick,  # Ingest→Schedule same tick
                                source_agent_id="human",
                                task_id=task_id,
                                details={
                                    "action": action,
                                    "external_id": ev.external_id,
                                },
                            )
                        )
                        self._audit_log.record(
                            AuditEventType.HUMAN_ACTION,
                            agent_id=assignee,
                            tick=tick,
                            details={
                                "action": action,
                                "task_id": task_id,
                                "external_id": ev.external_id,
                            },
                        )
                        continue
                # Unroutable human action (bad action, missing task, or
                # non-human assignee) — audit and drop.
                self._audit_log.record(
                    AuditEventType.HUMAN_ACTION,
                    tick=tick,
                    details={
                        "source": ev.source,
                        "event_type": ev.event_type,
                        "external_id": ev.external_id,
                        "status": "dropped_unroutable",
                        "payload": ev.payload,
                    },
                    success=False,
                    error="Unroutable human action",
                )
                continue
            # Standalone external event — advisory wake to subscribed agents
            self._audit_log.record(
                AuditEventType.TOOL_RESULT,
                agent_id="",
                tick=tick,
                details={
                    "source": ev.source,
                    "event_type": ev.event_type,
                    "external_id": ev.external_id,
                    "status": "ingressed",
                },
            )
            # Route to agents subscribed to this integration's
            # ingress_event_types via their WakeCondition.event_types.
            for agent_id, runtime_state in self._agent_runtime_states.items():
                cond = self._scheduler.get_wake_condition(agent_id)
                if cond is None:
                    continue
                matching = [t for t in cond.event_types if t == WakeEventType.EXTERNAL_RESULT]
                if not matching:
                    continue
                self._scheduler.enqueue_event(
                    WakeupEvent(
                        event_type=WakeEventType.EXTERNAL_RESULT,
                        target_agent_id=agent_id,
                        tick=tick,
                        visible_at_tick=tick,  # Ingest→Schedule same tick
                        source_agent_id=ev.source,
                        task_id="",
                        details={
                            "external_id": ev.external_id,
                            "event_type": ev.event_type,
                            "source": ev.source,
                            "result": ev.payload,
                        },
                    )
                )

        # Deliver any external ops that were just completed by receipts.
        for op in self._pending_ops.collect_completed(tick):
            rs2 = self._agent_runtime_states.get(op.agent_id)
            if rs2 is not None and rs2.continuation.pending_request_id == op.request_id:
                if op.op_type == OpType.TOOL_REQUEST:
                    if op.metadata.get("external_tool"):
                        rs2.receive_external_result(op.result, tick)
                        self._enqueue_result_wake(op, tick, result=op.result)
                    else:
                        rs2.receive_tool_result(op.result, tick)
                        self._enqueue_result_wake(op, tick, result=op.result)
                elif op.op_type == OpType.LLM_REQUEST:
                    rs2.receive_llm_result(op.result, tick)
                    self._enqueue_result_wake(op, tick, result=op.result)
                self._pending_ops.remove(op.request_id)

        # T11: real-time deadline monitoring (SPEC §9.2) — wake owners
        # with DEADLINE_APPROACHING / TIMER_EXPIRY (fire once per task
        # per kind; rollback un-marks, see run_tick).
        self._check_deadlines(tick)

        # T11: calendar rules (SPEC §9.1) — stage RULE_ADVANCE (+ task
        # creation) for due rules; wakes are enqueued post-commit only.
        self._check_calendar(tick)

        # T11 决策 3: deferred-mode WorkerPool dispatch — pair pending
        # umbrella tasks with idle children, staged atomically.
        self._dispatch_deferred_pools(tick)

    def _select_pool_child(
        self,
        manager_id: str,
        strategy: Any,
        skill: str | None = None,
    ) -> str | None:
        """Declarative child selection for a pool manager (T11 决策 3).

        All strategies are kernel-executed rules on the manager's
        children — no LLM involvement. Returns None when the pool has
        no children.
        """
        from my_team.models.agent import PoolStrategy

        child_ids = sorted(self._agent_tree.child_ids(manager_id))
        if not child_ids:
            return None
        if strategy == PoolStrategy.SKILL_MATCH and skill:
            matched = [cid for cid in child_ids if self._child_has_skill(cid, skill)]
            if matched:
                child_ids = matched
            # No match → fall through to least_busy over all children.
        if strategy == PoolStrategy.ROUND_ROBIN and len(child_ids) > 0:
            cursor = self._pool_cursors.get(manager_id, 0)
            self._pool_cursors[manager_id] = (cursor + 1) % len(child_ids)
            return child_ids[cursor]
        # least_busy (default; also the skill_match fallback):
        return min(
            child_ids,
            key=lambda cid: (
                sum(1 for t in self._task_tree.get_assignee_tasks(cid) if t.is_active),
                cid,
            ),
        )

    def _child_has_skill(self, agent_id: str, skill: str) -> bool:
        config = self._agent_tree.get(agent_id)
        skills = config.metadata.get("skills", [])
        return config.role == skill or skill in skills

    def _dispatch_deferred_pools(self, tick: int) -> None:
        """Deferred-mode pool dispatch (T11 决策 3).

        Pending work is derived statelessly — assignee == manager ∧
        ASSIGNED ∧ no copy derived from it — so no extra queue entity
        exists. Each tick, Ingest pairs pending tasks with idle
        children (no active tasks) and stages each dispatch as an
        atomic group (copy + notification email). Assignment stays a
        single-point serial decision of the manager (kernel-executed
        rule), same-tick commit-atomic: no claim races.
        """
        from my_team.models.agent import PoolMode

        for config in self._agent_tree:
            if config.kind != "service" or config.pool is None:
                continue
            if config.pool.mode != PoolMode.DEFERRED:
                continue
            manager_id = config.agent_id
            owned = self._task_tree.get_assignee_tasks(manager_id)
            dispatched = {t.derived_from for t in self._task_tree if t.derived_from is not None}
            pending = [
                t for t in owned if t.status == TaskStatus.ASSIGNED and t.task_id not in dispatched
            ]
            idle_children = [
                cid
                for cid in sorted(self._agent_tree.child_ids(manager_id))
                if not any(t.is_active for t in self._task_tree.get_assignee_tasks(cid))
            ]
            for task, child in zip(pending, idle_children):
                from uuid import uuid4

                copy_id = f"task.{tick}.{uuid4().hex[:8]}"
                group_id = f"pool.{manager_id}.{tick}.{copy_id}"
                self._transaction_buffer.stage(
                    effect_type=EffectType.TASK_CREATE,
                    agent_id=manager_id,
                    resource=copy_id,
                    data={
                        "task_id": copy_id,
                        "title": task.title,
                        "description": task.description,
                        "assigner_agent_id": manager_id,
                        "assignee_agent_id": child,
                        "derived_from": task.task_id,
                        "priority": task.priority.value,
                        "deadline": task.deadline,
                    },
                    group_id=group_id,
                    atomicity="group",
                )
                self._transaction_buffer.stage(
                    effect_type=EffectType.EMAIL_SEND,
                    agent_id=manager_id,
                    resource=f"email:{manager_id}",
                    data={
                        "from_agent": manager_id,
                        "to": [child],
                        "subject": f"[POOL] {task.title}",
                        "body": task.description,
                        "email_type": "delegation",
                        "task_id": copy_id,
                    },
                    group_id=group_id,
                    atomicity="group",
                )

    def register_schedule_rule(self, rule: ScheduleRule) -> ScheduleRule:
        """Register a calendar rule (SPEC §9.1). Validates target and
        stamps ``registered_at`` with the current business time; the
        first cron fire is the next occurrence strictly after it."""
        if rule.target_agent_id not in self._agent_tree:
            raise ValueError(
                f"Schedule rule '{rule.rule_id}' targets unknown agent '{rule.target_agent_id}'",
            )
        rule.registered_at = self._tick_engine.wall_now()
        return self._calendar_store.register(rule)

    def _check_calendar(self, tick: int) -> None:
        """Evaluate due schedule rules and stage their effects.

        Due-ness is read-only evaluation against the business clock;
        the advancement itself is a staged RULE_ADVANCE effect grouped
        atomically with any created task — commit applies both or the
        rollback inverts both (T11 决策 1). Wake events for EMIT_EVENT
        rules are enqueued post-commit (Publish), never on a rolled
        back tick.
        """
        now = self._tick_engine.wall_now()
        for rule in self._calendar_store.enabled():
            fire: dict[str, Any] | None = None
            if rule.interval_ticks is not None:
                if tick < rule.next_run_tick:
                    continue
                fire = {
                    "rule_id": rule.rule_id,
                    "next_run_tick": tick + rule.interval_ticks,
                    "last_fired_at": None,  # interval rules: unchanged
                }
            else:
                assert rule.cron is not None
                base = rule.last_fired_at or rule.registered_at or now
                next_fire = rule.cron.next_fire_after(base)
                if now < next_fire:
                    continue
                fire = {
                    "rule_id": rule.rule_id,
                    "next_run_tick": None,
                    "last_fired_at": next_fire,
                }
            assert fire is not None

            group_id = f"cal.{rule.rule_id}.{tick}"
            self._transaction_buffer.stage(
                effect_type=EffectType.RULE_ADVANCE,
                agent_id=rule.target_agent_id,
                resource=rule.rule_id,
                data={
                    "rule_id": rule.rule_id,
                    "next_run_tick": fire["next_run_tick"],
                    "last_fired_at": fire["last_fired_at"],
                },
                group_id=group_id,
                atomicity="group",
            )
            if rule.action == ScheduleAction.CREATE_TASK and rule.task_template is not None:
                from uuid import uuid4

                task_id = f"task.{tick}.{uuid4().hex[:8]}"
                template = rule.task_template
                deadline = (
                    now + timedelta(minutes=template.deadline_offset_minutes)
                    if template.deadline_offset_minutes is not None
                    else None
                )
                self._transaction_buffer.stage(
                    effect_type=EffectType.TASK_CREATE,
                    agent_id=rule.target_agent_id,
                    resource=task_id,
                    data={
                        "task_id": task_id,
                        "title": template.title,
                        "description": template.description,
                        "assigner_agent_id": "system:calendar",
                        "assignee_agent_id": rule.target_agent_id,
                        "priority": template.priority.value,
                        "deadline": deadline,
                    },
                    group_id=group_id,
                    atomicity="group",
                )
            self._calendar_fires_this_tick.append(fire)

    def _check_deadlines(self, tick: int) -> None:
        """Scan active tasks against the business wall clock.

        For each non-terminal task with a real-time ``deadline``:
        - ``now >= deadline`` → TIMER_EXPIRY to the assignee;
        - ``deadline - threshold <= now < deadline`` →
          DEADLINE_APPROACHING to the assignee.
        Each kind fires once per task; a rolled-back tick un-marks its
        fires so nothing is lost on re-execution.
        """
        now = self._tick_engine.wall_now()
        threshold = self._tick_engine.config.deadline_approaching_ticks * (
            self._tick_engine.config.tick_duration_timedelta
        )
        for task in self._task_tree.get_active_tasks():
            if task.deadline is None:
                continue
            fired = self._deadline_fired.setdefault(task.task_id, set())
            if now >= task.deadline:
                kind = "expired"
            elif now >= task.deadline - threshold:
                kind = "approaching"
            else:
                continue
            if kind in fired:
                continue
            fired.add(kind)
            self._deadline_fired_this_tick.append((task.task_id, kind))
            assignee = task.assignee_agent_id
            runtime_state = self._agent_runtime_states.get(assignee)
            if runtime_state is None:
                continue  # human/service targets without runtime state
            self._scheduler.enqueue_event(
                WakeupEvent(
                    event_type=(
                        WakeEventType.TIMER_EXPIRY
                        if kind == "expired"
                        else WakeEventType.DEADLINE_APPROACHING
                    ),
                    target_agent_id=assignee,
                    tick=tick,
                    visible_at_tick=tick,  # Ingest→Schedule same tick
                    source_agent_id="system",
                    task_id=task.task_id,
                    details={
                        "deadline": task.deadline.isoformat(),
                        "now": now.isoformat(),
                        "kind": kind,
                    },
                )
            )
            self._audit_log.record(
                AuditEventType.AGENT_WOKEN,
                agent_id=assignee,
                tick=tick,
                details={
                    "task_id": task.task_id,
                    "wake": kind,
                    "deadline": task.deadline.isoformat(),
                },
            )

    def _unmark_deadline_fires(self) -> None:
        """Rollback recovery: un-mark deadline events fired this tick."""
        for task_id, kind in self._deadline_fired_this_tick:
            fired = self._deadline_fired.get(task_id)
            if fired is not None:
                fired.discard(kind)
                if not fired:
                    self._deadline_fired.pop(task_id, None)
        self._deadline_fired_this_tick = []

    def _enqueue_result_wake(
        self,
        op: PendingOperation,
        tick: int,
        result: dict[str, Any],
    ) -> None:
        """Publish a TOOL_RESULT wake event for a delivered op result."""
        if op.op_type == OpType.LLM_REQUEST:
            self._scheduler.enqueue_event(
                WakeupEvent(
                    event_type=WakeEventType.TOOL_RESULT,  # reuse TOOL_RESULT
                    target_agent_id=op.agent_id,
                    tick=tick,
                    visible_at_tick=tick,  # Ingest→Schedule same tick
                    source_agent_id="llm_gateway",
                    task_id=op.task_id,
                    details={
                        "request_id": op.request_id,
                        "result_type": "llm_result",
                        "result": result,
                    },
                )
            )
        elif op.op_type == OpType.TOOL_REQUEST:
            # T9: an external-owned outbound op wakes the agent with an
            # EXTERNAL_RESULT event (决策3 — 纯事件). Non-external tools
            # keep the existing TOOL_RESULT path.
            if op.metadata.get("external_tool"):
                self._scheduler.enqueue_event(
                    WakeupEvent(
                        event_type=WakeEventType.EXTERNAL_RESULT,
                        target_agent_id=op.agent_id,
                        tick=tick,
                        visible_at_tick=tick,  # Ingest→Schedule same tick
                        source_agent_id=op.metadata.get("provider", "external"),
                        task_id=op.task_id,
                        details={
                            "request_id": op.request_id,
                            "result_type": "external_result",
                            "result": result,
                        },
                    )
                )
                return
            self._scheduler.enqueue_event(
                WakeupEvent(
                    event_type=WakeEventType.TOOL_RESULT,
                    target_agent_id=op.agent_id,
                    tick=tick,
                    visible_at_tick=tick,  # Ingest→Schedule same tick
                    source_agent_id="tool_executor",
                    task_id=op.task_id,
                    details={
                        "request_id": op.request_id,
                        "result_type": "tool_result",
                        "result": result,
                    },
                )
            )

    def _get_agent_states(self) -> dict[str, AgentState]:
        """Get current state of all agents for scheduler.

        Uses AgentRuntimeState as the authoritative source.
        """
        return {aid: rs.state for aid, rs in self._agent_runtime_states.items()}

    def _agent_urgency(self, agent_id: str) -> tuple[int, Any]:
        """Most urgent active task of an agent — SLA sort key
        (T11 决策 2): highest priority, then earliest real-time
        deadline. Returns (rank, deadline); (-1, None) if none."""
        best_rank = -1
        best_deadline: Any = None
        for t in self._task_tree.get_assignee_tasks(agent_id):
            if t.is_terminal:
                continue
            rank = _TASK_PRIORITY_RANK[t.priority]
            if rank > best_rank:
                best_rank = rank
                best_deadline = t.deadline
            elif rank == best_rank and t.deadline is not None:
                if best_deadline is None or t.deadline < best_deadline:
                    best_deadline = t.deadline
        return best_rank, best_deadline

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
                    self._scheduler.enqueue_event(
                        WakeupEvent(
                            event_type=WakeEventType.BOOTSTRAP,
                            target_agent_id=agent_config.agent_id,
                            tick=tick,
                            visible_at_tick=tick,  # immediate visibility
                            source_agent_id="system",
                        )
                    )

        agent_states = self._get_agent_states()
        ready = self._scheduler.compute_ready_set(
            tick,
            agent_states,
            urgency=self._agent_urgency,
        )

        # Capacity-deferred agents (T11 决策 2): explainable per
        # SPEC §14 — audited so overload behavior is always visible.
        for cand in self._scheduler.last_overflow:
            self._audit_log.record(
                AuditEventType.AGENT_CAPACITY_DEFERRED,
                agent_id=cand.agent_id,
                tick=tick,
                details={
                    "event_count": len(cand.events),
                    "reason": "max_active_agents_per_tick",
                },
            )

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
            assignee_tasks = self._task_tree.get_assignee_tasks(agent_id)

            agent_states[agent_id] = {
                "config": agent_config.model_dump(),
                "inbox_unread": mailbox.unread_count if mailbox else 0,
                "tasks": {
                    t.task_id: {
                        "status": t.status.value,
                        "title": t.title,
                        "assignee": t.assignee_agent_id,
                    }
                    for t in assignee_tasks
                },
            }

        # Per-agent private file INDEX (SPEC §3.1 冻结视图按需化): metadata
        # only — paths and sizes/mtimes, NEVER file contents. Tools read
        # contents on demand (committed disk state + own staged), so no
        # full-content snapshot is built.
        private_files: dict[str, dict[str, Any]] = {}
        for agent_config in self._agent_tree:
            agent_id = agent_config.agent_id
            home = self._private_store.agent_home(agent_id)
            files: dict[str, Any] = {}
            dirs: list[str] = []
            if home.exists():
                for p in sorted(home.rglob("*")):
                    rel = p.relative_to(home).as_posix()
                    if p.is_file():
                        try:
                            st = p.stat()
                        except OSError:
                            continue
                        files[rel] = {"size": st.st_size, "mtime": st.st_mtime}
                    else:
                        dirs.append(rel)
            private_files[agent_id] = {"files": files, "dirs": dirs}

        # Per-agent workspace version (v0.8.0 P1-3): hash of the file
        # INDEX (metadata only, not contents). A ToolRequest records the
        # version it was based on; apply-time FILE_PATCH base-hash
        # checks remain the content-level enforcement.
        workspace_versions: dict[str, str] = {}
        for agent_config in self._agent_tree:
            agent_id = agent_config.agent_id
            workspace_versions[agent_id] = hash_payload(
                private_files[agent_id],
            )

        # Get pending emails
        pending_emails = []
        for mailbox in [self._mail_system.get_mailbox(aid) for aid in self._agent_tree.all_ids]:
            if mailbox:
                for email in mailbox.inbox:
                    if email.status.value == "delivered":
                        pending_emails.append(
                            {
                                "email_id": email.email_id,
                                "from": email.from_agent,
                                "to": email.to,
                                "subject": email.subject,
                                "email_type": email.email_type.value,
                                "task_id": email.task_id,
                                "body": email.body,
                                # v0.10 T8b: attachment refs visible to the
                                # recipient's context (清单, not payload)
                                "attachments": [
                                    {
                                        "ref_type": a.ref_type,
                                        "path": a.path,
                                        "version": a.version,
                                        "hash": a.hash,
                                        "size": a.size,
                                        "mime": a.mime,
                                    }
                                    for a in email.attachments
                                ],
                            }
                        )

        return {
            "tick": tick,
            "agents": agent_states,
            "emails": pending_emails,
            "workspace_versions": workspace_versions,
            "shared_kb": {
                "paths": self._shared_kb.all_paths(),
                "versions": {
                    p: v.version for p, v in self._shared_kb.versions.all_versions().items()
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
                lock.resource: lock.lock_token for lock in self._lock_manager.active_locks()
            },
            "tasks": {
                t.task_id: {
                    "status": t.status.value,
                    "title": t.title,
                    "assignee": t.assignee_agent_id,
                    "assigner": t.assigner_agent_id,
                }
                for t in self._task_tree
            },
            "private_files": private_files,
        }

    def _phase_deliver(self, tick: int) -> list[Email]:
        """Phase 2: Deliver emails and generate NEW_EMAIL wake events.

        Wake events are enqueued during Deliver (before Schedule), so
        they are visible in the same tick's Schedule phase.
        """
        delivered = self._mail_system.deliver(tick)
        # Generate wake events for recipients — visible this tick
        for email in delivered:
            for recipient in email.to:
                self._scheduler.enqueue_event(
                    WakeupEvent(
                        event_type=WakeEventType.NEW_EMAIL,
                        target_agent_id=recipient,
                        tick=tick,
                        visible_at_tick=tick,  # same-tick visibility
                        source_agent_id=email.from_agent,
                        task_id=email.task_id or "",
                        thread_id=email.thread_id or "",
                        details={"email_id": email.email_id},
                    )
                )
        return delivered

    def _phase_observe(
        self,
        tick: int,
        snapshot: dict[str, Any],
        ready: list[ReadyCandidate] | None = None,
    ) -> dict[str, AgentObservation]:
        """Phase 4: Ready agents observe the frozen snapshot.

        T6: Uses ContextCompiler for role-aware observation assembly
        with token budget enforcement.
        """
        observations: dict[str, AgentObservation] = {}
        # Determine which agents to observe
        if ready is not None:
            active_ids = {c.agent_id for c in ready}
        else:
            active_ids = set(self._runtimes.keys())

        for agent_id, runtime in self._runtimes.items():
            if agent_id not in active_ids:
                continue

            # Find agent config
            agent_config = None
            for cfg in self._agent_tree:
                if cfg.agent_id == agent_id:
                    agent_config = cfg
                    break
            if agent_config is None:
                continue

            # T6: Use ContextCompiler for role-aware observation
            continuation = self._agent_runtime_states[agent_id].continuation
            compiled = self._context_compiler.compile(
                agent_config,
                snapshot,
                continuation,
            )

            # Wrap in AgentObservation (backward compatible)
            # T12a: inject pending human UI actions (kind=human only —
            # empty for everyone else).
            observations[agent_id] = AgentObservation(
                agent_id=compiled["agent_id"],
                tick=compiled["tick"],
                emails=compiled.get("emails", []),
                task_states=compiled.get("task_states", {}),
                shared_kb_snapshot=compiled.get("shared_kb_snapshot", {}),
                lock_states=compiled.get("lock_states", {}),
                private_workspace_path=compiled.get("private_workspace_path", ""),
                pending_human_actions=list(self._pending_human_actions.get(agent_id, [])),
                # N4-3 注入布局元数据（Observe 只读消费；N4-4 CONSOLIDATING
                # 预算触发数据源：pending_consolidation + fixed_usage_ratio）
                memory_injection=compiled.get("memory_injection", {}),
            )
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
                # N4-4：CONSOLIDATING 预算触发/回落（hysteresis 进 90%/
                # 出 80%）。Observe 只读消费 pending_consolidation 与
                # fixed_usage_ratio，相位迁移在 decide/act（写路径）。
                # 会话标记 = continuation.resume_phase（会话跨越
                # CONSOLIDATING / WAITING_* / PROCESSING_RESULT 等相位）。
                # 有未处理的 LLM/工具结果时先处理结果（PROCESSING_RESULT
                # 优先），不抢占结果处理路径。
                pending_result = (
                    continuation.phase == ContinuationPhase.PROCESSING_RESULT
                    and bool(continuation.last_llm_result or continuation.last_tool_result)
                )
                if not pending_result:
                    mi = obs.memory_injection or {}
                    in_session = continuation.resume_phase is not None
                    if (
                        not in_session
                        and self._consolidation_gate.should_enter(
                            pending_consolidation=mi.get("pending_consolidation", False),
                            usage_ratio=mi.get("fixed_usage_ratio", 0.0),
                            active_intent=False,
                        )
                    ):
                        continuation.enter_consolidating(tick)
                    elif (
                        in_session
                        and self._consolidation_gate.should_exit(
                            usage_ratio=mi.get("fixed_usage_ratio", 0.0),
                        )
                    ):
                        continuation.exit_consolidating(tick)
                intents[agent_id] = runtime.decide_intents(
                    obs,
                    continuation=continuation,
                )
                # If the agent just processed a pending result, finalize
                if continuation.phase == ContinuationPhase.PROCESSING_RESULT:
                    continuation.finalize_result_processing(tick)
                # T12a: consumed human UI actions — moved out of the
                # pending queue (observed above) so they translate once.
                # Recorded for rollback restore (no lost action).
                consumed = self._pending_human_actions.pop(agent_id, None)
                if consumed:
                    for act in consumed:
                        self._human_actions_consumed_this_tick.append(
                            (agent_id, act),
                        )
        return intents

    def _phase_act(
        self,
        tick: int,
        plans: dict[str, list[Intent]],
        ready: list[ReadyCandidate] | None = None,
        validated: dict[str, list[ActionResult]] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, list[ActionResult]]:
        """Phase 7: Convert validated Intents into staged effects.

        Act REGISTERS and STAGES but never applies: effects are applied
        in Phase 8 (Commit), pending ops are submitted to the registry.

        Only intents that passed validation in Phase 6 are processed.
        Each intent type maps to:

        - SubmitLLMRequest   → register PendingOperation, agent → WAITING_FOR_LLM
        - SubmitToolRequest  → local tools (read/ls) execute against the
                               frozen snapshot view; remote tools register
                               PendingOperation
        - SendEmailIntent    → stage EMAIL_SEND effect
        - DelegateIntent     → stage TASK_CREATE + EMAIL_SEND effects
        - WritePrivateFileIntent → stage FILE_WRITE effect
        - WaitForEventIntent → agent → waiting state
        """
        all_results: dict[str, list[ActionResult]] = {}
        # LLM submissions per agent THIS tick — commit-time budget
        # re-check: PreValidate counted the registry (which does not yet
        # include this tick's submissions), so two agents could both
        # pass; this closes the window before ops are submitted.
        submitted_llm: dict[str, int] = {}
        # P0-2: reset this-tick tracking for rollback
        self._tick_pending_ops.clear()
        self._tick_continuations.clear()
        self._tick_acquired_locks.clear()

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
                    # Commit-time budget re-check (配额仍够): registry
                    # in-flight + this tick's submissions for this agent
                    in_flight = self._pending_ops.count_in_flight(
                        agent_id,
                        op_type=OpType.LLM_REQUEST,
                    ) + submitted_llm.get(agent_id, 0)
                    if in_flight >= self._config.max_concurrent_llm_requests:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    f"LLM budget exceeded for '{agent_id}' "
                                    f"at commit time ({in_flight} in flight, "
                                    f"max "
                                    f"{self._config.max_concurrent_llm_requests})"
                                ),
                            )
                        )
                        continue
                    submitted_llm[agent_id] = submitted_llm.get(agent_id, 0) + 1
                    op = self._pending_ops.submit(
                        op_type=OpType.LLM_REQUEST,
                        agent_id=agent_id,
                        created_tick=tick,
                        eligible_tick=tick + 1,
                        deadline_tick=tick + intent.timeout_ticks,
                        task_id=intent.task_id,
                        state_epoch=self._state_epoch,
                        metadata={
                            "request_id": intent.request_id,
                            "model": intent.model,
                            "messages": [
                                m.model_dump() if hasattr(m, "model_dump") else m
                                for m in intent.messages
                            ],
                            "tools": [
                                t.model_dump() if hasattr(t, "model_dump") else t
                                for t in intent.tools
                            ],
                            "temperature": intent.temperature,
                            "max_tokens": intent.max_tokens,
                        },
                    )
                    if runtime_state:
                        # P0-2: snapshot continuation before mutation
                        c = runtime_state.continuation
                        self._tick_continuations[agent_id] = (
                            c.phase,
                            c.pending_request_id,
                            c.pending_request_type,
                        )
                        c.advance_to_waiting_llm(op.request_id, tick)
                        runtime_state.transition_to_waiting(
                            AgentState.WAITING_FOR_LLM,
                            tick,
                        )
                    # P0-2: track op for rollback
                    self._tick_pending_ops.append((agent_id, op))
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={
                                "request_id": op.request_id,
                                "status": "pending",
                            },
                        )
                    )
                    continue

                # SubmitToolRequest → local tools execute, remote register
                if isinstance(intent, SubmitToolRequest):
                    # Routing by execution class (v0.8.0 P1-4/5), not a
                    # hardcoded name list: PURE/READ_ONLY/STAGED_MUTATION
                    # tools are kernel-executed at Act (frozen view);
                    # LOCAL_PROCESS/SANDBOXED_PROCESS/EXTERNAL_IRREVERSIBLE
                    # tools become pending ops and go through Executor
                    # Admission + dispatch in Phase 9.
                    manifest = self._tool_registry.get_manifest(
                        intent.tool_name,
                    )
                    kernel_executed = manifest is not None and not requires_executor(
                        manifest.execution_class
                    )
                    if kernel_executed:
                        # Local tools execute synchronously; file tools
                        # read on demand (committed state + own staged)
                        # — no frozen view (SPEC §3.1 按需化).
                        runtime = self._runtimes.get(agent_id)
                        if runtime:
                            # N1b（§5.1）：context 不再携带 allowed_tools；
                            # 授权求值在 execute 内走 Authority 两层 Grant。
                            tool_context = ToolContext(
                                agent_id=agent_id,
                                tick=tick,
                            )
                            tr = self._tool_registry.execute(
                                context=tool_context,
                                tool_name=intent.tool_name,
                                **intent.arguments,
                            )
                            results.append(
                                ActionResult(
                                    action=action,
                                    success=tr.success,
                                    result_data=tr.data,
                                    error=tr.error,
                                )
                            )
                        else:
                            results.append(
                                ActionResult(
                                    action=action,
                                    success=False,
                                    error=f"No runtime for '{agent_id}'",
                                )
                            )
                    else:
                        # Remote tool → register pending operation with
                        # a system-built ToolRequest (v0.8.0 P1-3). All
                        # identity/version/hash/epoch fields are
                        # injected by the kernel here — an executor or
                        # plugin never supplies them.
                        op = self._pending_ops.submit(
                            op_type=OpType.TOOL_REQUEST,
                            agent_id=agent_id,
                            created_tick=tick,
                            eligible_tick=tick + 1,
                            deadline_tick=tick + intent.timeout_ticks,
                            task_id=intent.task_id,
                            state_epoch=self._state_epoch,
                            metadata={
                                "request_id": intent.request_id,
                                "tool_name": intent.tool_name,
                                "arguments": intent.arguments,
                            },
                        )
                        manifest = self._tool_registry.get_manifest(
                            intent.tool_name,
                        )
                        if manifest is not None:
                            op.tool_request = ToolRequest(
                                request_id=op.request_id,
                                agent_id=agent_id,
                                task_id=intent.task_id,
                                tool_name=intent.tool_name,
                                tool_version=manifest.version,
                                manifest_hash=manifest.manifest_hash,
                                input_hash=hash_payload(intent.arguments),
                                state_epoch=self._state_epoch,
                                workspace_version=(
                                    (snapshot or {})
                                    .get("workspace_versions", {})
                                    .get(agent_id, "0")
                                ),
                                created_tick=tick,
                                deadline_tick=tick + intent.timeout_ticks,
                                arguments=dict(intent.arguments),
                            )
                            # Reads happen on demand at dispatch
                            # (committed state + own staged) — no view
                            # is bound at submission (SPEC §3.1 按需化).
                        if runtime_state:
                            # P0-2: snapshot continuation before mutation
                            c = runtime_state.continuation
                            self._tick_continuations[agent_id] = (
                                c.phase,
                                c.pending_request_id,
                                c.pending_request_type,
                            )
                            # T9: an outbound tool owned by an Integration
                            # parks the agent in WAITING_FOR_EXTERNAL (决策3 —
                            # 纯事件等待，不乐观回查) until the external op
                            # completes or times out. Kernel-internal remote
                            # tools keep the existing WAITING_FOR_TOOL path.
                            if (
                                self._integrations.get_by_tool(
                                    intent.tool_name,
                                )
                                is not None
                            ):
                                op.metadata["external_tool"] = True
                                op.metadata["provider"] = self._integrations.provider_for_tool(
                                    intent.tool_name
                                )
                                c.advance_to_waiting_external(
                                    op.request_id,
                                    tick,
                                )
                                runtime_state.transition_to_waiting(
                                    AgentState.WAITING_FOR_EXTERNAL,
                                    tick,
                                )
                            else:
                                c.advance_to_waiting_tool(op.request_id, tick)
                                runtime_state.transition_to_waiting(
                                    AgentState.WAITING_FOR_TOOL,
                                    tick,
                                )
                        # P0-2: track op for rollback
                        self._tick_pending_ops.append((agent_id, op))
                        results.append(
                            ActionResult(
                                action=action,
                                success=True,
                                result_data={
                                    "request_id": op.request_id,
                                    "status": "pending",
                                },
                            )
                        )
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
                            # v0.10 T8b: attachment refs carried on the
                            # email (never copied)
                            "attachments": [
                                AttachmentRef.model_validate(a) for a in intent.attachments
                            ],
                        },
                    )
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"staged": True},
                        )
                    )
                    continue

                # DelegateIntent → stage TASK_CREATE + EMAIL_SEND.
                # GROUP ATOMICITY: the delegation is one logical
                # operation — if the task creation fails validation,
                # the delegation email must NOT be sent (and vice
                # versa). Members share group_id = the intent's id.
                if isinstance(intent, DelegateIntent):
                    from uuid import uuid4

                    task_id = f"task.{tick}.{uuid4().hex[:8]}"
                    recipient = self._agent_tree.get(
                        intent.recipient_agent_id,
                    )
                    # T11 决策 3: delegation into an immediate-mode
                    # WorkerPool expands here — original (assignee =
                    # pool manager) + working copy (assignee = selected
                    # child, derived_from = original) + worker notice,
                    # all one atomic group; zero wake latency.
                    pool_copy_id: str | None = None
                    pool_child: str | None = None
                    if (
                        recipient.kind == "service"
                        and recipient.pool is not None
                        and recipient.pool.mode == PoolMode.IMMEDIATE
                    ):
                        pool_child = self._select_pool_child(
                            intent.recipient_agent_id,
                            recipient.pool.strategy,
                            skill=intent.skill,
                        )
                        if pool_child is None:
                            results.append(
                                ActionResult(
                                    action=action,
                                    success=False,
                                    error=(
                                        f"WorkerPool '{intent.recipient_agent_id}' has no workers"
                                    ),
                                    error_code="INVALID_ARGUMENT",
                                )
                            )
                            continue
                        pool_copy_id = f"task.{tick}.{uuid4().hex[:8]}"
                    self._transaction_buffer.stage(
                        effect_type=EffectType.TASK_CREATE,
                        agent_id=agent_id,
                        resource=task_id,
                        data={
                            "task_id": task_id,
                            "title": intent.task_title,
                            "description": intent.task_description,
                            "assigner_agent_id": agent_id,
                            "assignee_agent_id": intent.recipient_agent_id,
                            "derived_from": intent.derived_from or None,
                            "deadline": intent.deadline,
                        },
                        group_id=intent.intent_id,
                        atomicity="group",
                    )
                    if pool_copy_id is not None and pool_child is not None:
                        assert pool_child is not None
                        self._transaction_buffer.stage(
                            effect_type=EffectType.TASK_CREATE,
                            agent_id=agent_id,
                            resource=pool_copy_id,
                            data={
                                "task_id": pool_copy_id,
                                "title": intent.task_title,
                                "description": intent.task_description,
                                "assigner_agent_id": (intent.recipient_agent_id),
                                "assignee_agent_id": pool_child,
                                "derived_from": task_id,
                                "priority": "normal",
                                "deadline": intent.deadline,
                            },
                            group_id=intent.intent_id,
                            atomicity="group",
                        )
                    self._transaction_buffer.stage(
                        effect_type=EffectType.EMAIL_SEND,
                        agent_id=agent_id,
                        resource=f"email:{agent_id}",
                        data={
                            "from_agent": agent_id,
                            "to": [pool_child or intent.recipient_agent_id],
                            "subject": f"[DELEGATE] {intent.task_title}",
                            "body": intent.task_description,
                            "email_type": "delegation",
                            "task_id": pool_copy_id or task_id,
                        },
                        group_id=intent.intent_id,
                        atomicity="group",
                    )
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"task_id": task_id, "staged": True},
                        )
                    )
                    continue

                # WritePrivateFileIntent → stage FILE_WRITE
                if isinstance(intent, WritePrivateFileIntent):
                    err = self._validate_write_path(intent.path)
                    if err is not None:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=err,
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                        continue
                    self._transaction_buffer.stage(
                        effect_type=EffectType.FILE_WRITE,
                        agent_id=agent_id,
                        resource=intent.path,
                        data={"content": intent.content},
                    )
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"staged": True},
                        )
                    )
                    continue

                # N4-4：MemoryConsolidateIntent → CONSOLIDATING 相位迁移
                # （写路径）。enter：置相位（下 tick 生效，本 tick 的 LLM
                # 请求已在 Observe 后定型）；exit：结构化摘要写入 MemoryEntry
                # （provenance 记整理来源）+ 恢复 resume_phase（被打断的
                # 工作立即续上）。会话标记 = resume_phase（处理完整理响应
                # 后 phase 可能已回落 READY_TO_DECIDE）。
                if isinstance(intent, MemoryConsolidateIntent):
                    runtime_state = self._agent_runtime_states.get(agent_id)
                    cont = runtime_state.continuation if runtime_state else None
                    if intent.action == "enter":
                        if cont is not None and cont.resume_phase is None:
                            cont.enter_consolidating(tick)
                        results.append(
                            ActionResult(
                                action=action,
                                success=True,
                                result_data={
                                    "phase": (
                                        cont.phase.value if cont is not None else "unknown"
                                    ),
                                },
                            )
                        )
                    elif intent.action == "exit":
                        if cont is not None and cont.resume_phase is not None:
                            if intent.structured_summary:
                                try:
                                    self._write_consolidation_summary(
                                        agent_id,
                                        intent.structured_summary,
                                        tick,
                                    )
                                except Exception:  # noqa: BLE001
                                    results.append(
                                        ActionResult(
                                            action=action,
                                            success=False,
                                            error="摘要写入失败",
                                        )
                                    )
                                    continue
                            cont.exit_consolidating(tick)
                        results.append(
                            ActionResult(
                                action=action,
                                success=True,
                                result_data={
                                    "phase": (
                                        cont.phase.value if cont is not None else "unknown"
                                    ),
                                },
                            )
                        )
                    else:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=f"未知 memory_consolidate action: {intent.action}",
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                    continue

                # WaitForEventIntent → agent waits for specific event
                if isinstance(intent, WaitForEventIntent):
                    if runtime_state:
                        waiting_state = AgentState(intent.waiting_state)
                        runtime_state.transition_to_waiting(waiting_state, tick)
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"waiting": intent.waiting_state},
                        )
                    )
                    continue

                # AcceptTaskIntent → stage TASK_UPDATE (accepted).
                # T12a: human worker accepts an assigned task — same
                # transaction path as any task-status update.
                if isinstance(intent, AcceptTaskIntent):
                    self._transaction_buffer.stage(
                        effect_type=EffectType.TASK_UPDATE,
                        agent_id=agent_id,
                        resource=intent.task_id,
                        data={"status": "accepted"},
                    )
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"task_id": intent.task_id, "staged": True},
                        )
                    )
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
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"task_id": intent.task_id, "staged": True},
                        )
                    )
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
                    results.append(
                        ActionResult(
                            action=action,
                            success=True,
                            result_data={"task_id": intent.task_id, "staged": True},
                        )
                    )
                    continue

                # Unknown intent — record as failure
                results.append(
                    ActionResult(
                        action=action,
                        success=False,
                        error=f"Unsupported intent type: {intent.intent_type}",
                    )
                )

            all_results[agent_id] = results

        return all_results

    # -- N4-4 整理模式辅助 ------------------------------------------------

    def _write_consolidation_summary(
        self,
        agent_id: str,
        summary_dict: dict[str, Any],
        tick: int,
    ) -> None:
        """把 CONSOLIDATING 结构化摘要写入 AgentMemory（Journal effect）。

        摘要作为 MemoryEntry（type=skill）写入，provenance 记整理来源
        （consolidation_origin）；索引同步（摘要条目可被链接词召回）。
        """
        from my_team.consolidation import ConsolidationSummary, write_summary_entry

        summary = ConsolidationSummary.model_validate(summary_dict)
        store = self._agent_memories.get(agent_id)
        engine = self._recall_engines.get(agent_id)
        if store is None or engine is None:
            raise RuntimeError(f"agent {agent_id} 无记忆子系统（未接线）")
        write_summary_entry(
            store,
            engine,
            summary,
            agent_id=agent_id,
            tick=tick,
            buffer=self._transaction_buffer,
        )

    def _phase_validate(
        self,
        tick: int,
        plans: dict[str, list[Intent]],
        ready: list[ReadyCandidate],
    ) -> dict[str, list[ActionResult]]:
        """Phase 6: PreValidate Intents before execution.

        Principle: PreValidate checks "is this attempt ALLOWED TO TRY?" —
        capability（两层 Grant，§3.5/§5.1）、policy、manifest、task
        validity。CommitValidate (Phase 8) separately checks "is it still
        COMMITTABLE now?" — locks, versions, task liveness, deadlines.
        PreValidate is
        side-effect-free: invalid intents become failed Results, valid
        intents pass through to Act for staging.

        Checks performed:
        1. LLM usage budget (T16c) — cumulative + this round's estimate
           vs per-agent/task/simulation caps (request_count / token /
           cost / wall_time) and concurrency vs in-flight. Over-limit
           rejects the WHOLE activation round (no partial execution).
        1b. Duplicate request_id — within plan + cross-tick (registry)
        1c. Tool capability（两层 Grant，§3.5/§5.1）+ deployment
            operation policy (v0.7.0)
        2. Delegation target — DelegateIntent targets direct children
        3. Payload fields — WritePrivateFileIntent has path,
           SendEmailIntent has to, DelegateIntent has recipient/title
        4. Task validity — referenced task exists, deadline not passed
        """
        validated: dict[str, list[ActionResult]] = {}

        for candidate in ready:
            agent_id = candidate.agent_id
            intent_list = plans.get(agent_id)
            if intent_list is None:
                continue

            results: list[ActionResult] = []

            # Budget pre-scan (T16c): if this round contains any LLM
            # request, verify the whole round stays within budget BEFORE
            # validating individual intents. Rejection is whole-round:
            # PreValidate failure == the round's validation failed ==
            # nothing executes, nothing commits (事务原子性 — no partial
            # LLM call happens). Judgment: 累计 + 本次请求估算.
            llm_intents = [i for i in intent_list if isinstance(i, SubmitLLMRequest)]
            if llm_intents:
                rejection = self._budget_rejection(agent_id, llm_intents)
                if rejection is not None:
                    for intent in intent_list:
                        action = AgentAction(
                            action_type=intent.intent_type.value,
                            tool_name=getattr(intent, "tool_name", ""),
                            payload=dict(intent.payload),
                        )
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=rejection.reason,
                                error_code="BUDGET_EXCEEDED",
                            )
                        )
                    validated[agent_id] = results
                    self._audit_budget_rejection(agent_id, rejection)
                    continue

            # request_ids seen in THIS plan — catches duplicates within
            # a single decide() (registry catches cross-tick duplicates)
            seen_request_ids: set[str] = set()

            for intent in intent_list:
                # Build a synthetic AgentAction for the result record
                action = AgentAction(
                    action_type=intent.intent_type.value,
                    tool_name=getattr(intent, "tool_name", ""),
                    payload=dict(intent.payload),
                )

                # Check 1c (first): duplicate request_id — an agent cannot
                # reuse a request_id that is already in flight (registry),
                # appears twice in the same plan (seen_request_ids), or
                # was EVER submitted before (persisted history — replay
                # protection across restart, v0.8.0 P1-6). 置于能力检查
                # 之前：重放的 request_id 无论当前 manifest/能力状态如何
                # 一律拒绝（身份去重优先于能力，§3.1 Ingest 去重语义）。
                if isinstance(intent, (SubmitLLMRequest, SubmitToolRequest)):
                    if intent.request_id and (
                        intent.request_id in seen_request_ids
                        or self._pending_ops.find_in_flight_request_id(
                            agent_id,
                            intent.request_id,
                        )
                        is not None
                        or self._pending_ops.is_seen(
                            agent_id,
                            intent.request_id,
                        )
                    ):
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    f"Duplicate request_id '{intent.request_id}' for '{agent_id}'"
                                ),
                                error_code="DUPLICATE_REQUEST_ID",
                            )
                        )
                        self._audit_log.record(
                            AuditEventType.PERMISSION_DENIED,
                            agent_id=agent_id,
                            tick=tick,
                            details={
                                "intent": intent.intent_type.value,
                                "request_id": intent.request_id,
                                "reason": "duplicate_request_id",
                                "error_code": "DUPLICATE_REQUEST_ID",
                            },
                            success=False,
                            error="Duplicate request_id rejected",
                        )
                        continue
                    seen_request_ids.add(intent.request_id)

                # Check 1: tool capability (SubmitToolRequest) — N1b 两层
                # Grant 求值（∃position：Grant(agent, position) ∧
                # Grant(position, entity_id)，§3.5/§5.1）。manifest 缺失 →
                # TOOL_MANIFEST_MISSING；manifest 存在但未授权 → 拒绝
                # （deny-by-default：未注册 uuid / 未授权工具调用被拒绝）。
                if isinstance(intent, SubmitToolRequest):
                    manifest = self._tool_registry.get_manifest(
                        intent.tool_name,
                    )
                    if manifest is None:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(f"Tool '{intent.tool_name}' has no registered manifest"),
                                error_code="TOOL_MANIFEST_MISSING",
                            )
                        )
                        self._audit_log.record(
                            AuditEventType.PERMISSION_DENIED,
                            agent_id=agent_id,
                            tick=tick,
                            details={
                                "tool": intent.tool_name,
                                "reason": "no_manifest",
                                "error_code": "TOOL_MANIFEST_MISSING",
                            },
                            success=False,
                            error="Tool has no manifest",
                        )
                        continue
                    capability = self._authority.authorize(
                        agent_id,
                        manifest.capability,
                    )
                    if not capability.allowed:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    f"Tool '{intent.tool_name}' not authorized for '{agent_id}'"
                                ),
                                error_code="CAPABILITY_DENIED",
                            )
                        )
                        self._audit_log.record(
                            AuditEventType.PERMISSION_DENIED,
                            agent_id=agent_id,
                            tick=tick,
                            details={
                                "tool": intent.tool_name,
                                "intent": intent.intent_type.value,
                                "error_code": "CAPABILITY_DENIED",
                            },
                            success=False,
                            error="Tool not authorized",
                        )
                        continue

                # Check 1d: deployment operation policy (v0.7.0).
                # PreValidate principle: "is this attempt allowed to try?"
                # — manifest 存在性已在 Check 1（两层 Grant）验证；此处
                # 只剩部署期 OperationPolicy（deny-by-default allowlist /
                # 审批）门禁（§3.5：allowlist/审批配置是数据）。
                if isinstance(intent, SubmitToolRequest):
                    decision = self._tool_registry.policy_decision(
                        intent.tool_name,
                    )
                    if not decision.allowed:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=decision.reason,
                                error_code="POLICY_DENIED",
                            )
                        )
                        self._audit_log.record(
                            AuditEventType.PERMISSION_DENIED,
                            agent_id=agent_id,
                            tick=tick,
                            details={
                                "tool": intent.tool_name,
                                "reason": "policy_denied",
                                "error_code": "POLICY_DENIED",
                            },
                            success=False,
                            error=decision.reason,
                        )
                        continue
                    if decision.requires_approval:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(f"Tool '{intent.tool_name}' requires human approval"),
                                error_code="APPROVAL_REQUIRED",
                            )
                        )
                        self._audit_log.record(
                            AuditEventType.PERMISSION_DENIED,
                            agent_id=agent_id,
                            tick=tick,
                            details={
                                "tool": intent.tool_name,
                                "reason": "requires_approval",
                                "error_code": "APPROVAL_REQUIRED",
                            },
                            success=False,
                            error="Tool requires human approval",
                        )
                        continue

                # Check 4: task validity + deadline (v0.7.0 hardening).
                # An intent referencing a task must reference an EXISTING
                # task; a task whose deadline has passed cannot be worked
                # on further (the TimeoutChecker expires it at Publish).
                if intent.task_id:
                    if not self._task_tree.exists(intent.task_id):
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(f"Task '{intent.task_id}' not found"),
                                error_code="TASK_NOT_FOUND",
                            )
                        )
                        continue
                    task = self._task_tree.get(intent.task_id)
                    now = self._tick_engine.wall_now()
                    if task.deadline is not None and task.deadline < now:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    f"Task '{intent.task_id}' deadline passed "
                                    f"(deadline={task.deadline.isoformat()} < "
                                    f"now={now.isoformat()})"
                                ),
                                error_code="DEADLINE_EXCEEDED",
                            )
                        )
                        continue

                # Check 2: delegation target validation
                if isinstance(intent, DelegateIntent):
                    target_id = intent.recipient_agent_id
                    if not self._agent_tree.can_delegate_to(agent_id, target_id):
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    f"'{agent_id}' cannot delegate to '{target_id}'"
                                    " (not a direct child)"
                                ),
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                        continue
                    # T11 决策 3: a bare kind=service proxy takes no
                    # delegated work — it must declare pool config.
                    target_cfg = self._agent_tree.get(target_id)
                    if target_cfg.kind == "service" and target_cfg.pool is None:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    f"'{target_id}' is a service agent without WorkerPool config"
                                ),
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                        continue

                # Check 3: required payload fields
                if isinstance(intent, WritePrivateFileIntent):
                    if not intent.path:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error="write intent requires 'path' field",
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                        continue

                if isinstance(intent, SendEmailIntent):
                    if not intent.to:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error="send_email intent requires 'to' field",
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                        continue

                if isinstance(intent, DelegateIntent):
                    if not intent.recipient_agent_id or not intent.task_title:
                        results.append(
                            ActionResult(
                                action=action,
                                success=False,
                                error=(
                                    "delegate intent requires 'recipient_agent_id' "
                                    "and 'task_title' fields"
                                ),
                                error_code="INVALID_ARGUMENT",
                            )
                        )
                        continue

                # Passed validation — will be staged in Act phase
                results.append(
                    ActionResult(
                        action=action,
                        success=True,
                        result_data={"validated": True},
                    )
                )

            validated[agent_id] = results

        return validated

    # -- T16c: LLM usage budget helpers -------------------------------------

    def _llm_in_flight_counts(self, agent_id: str, task_id: str) -> InFlightCounts:
        """Count in-flight (SUBMITTED/PENDING) LLM ops at each scope.

        PreValidate's concurrency gate: the pending-op registry does not
        yet include this round's submissions, so the round's own LLM
        requests are added by the caller via the estimate's
        ``request_count``.
        """
        agent_c = task_c = total_c = 0
        for op in self._pending_ops._operations.values():
            if op.op_type != OpType.LLM_REQUEST:
                continue
            if op.status not in {
                OpStatus.SUBMITTED,
                OpStatus.PENDING,
            }:
                continue
            total_c += 1
            if op.agent_id == agent_id:
                agent_c += 1
            if task_id and op.task_id == task_id:
                task_c += 1
        return InFlightCounts(agent=agent_c, task=task_c, simulation=total_c)

    def _budget_rejection(
        self,
        agent_id: str,
        llm_intents: list[SubmitLLMRequest],
    ) -> BudgetCheckResult | None:
        """Budget gate for one round: cumulative + estimate vs caps.

        Returns the first exceeded limit, or None if the round's LLM
        requests may proceed. Concurrency uses in-flight + this round's
        request count; the per-agent concurrency cap falls back to
        ``max_concurrent_llm_requests`` when the budget config leaves it
        at 0.
        """
        tick_duration_seconds = self._tick_engine.config.tick_duration_timedelta.total_seconds()
        task_id = next(
            (i.task_id for i in llm_intents if i.task_id),
            "",
        )
        estimate = BudgetUsage()
        for intent in llm_intents:
            estimate = estimate.add(
                estimate_llm_usage(
                    model=intent.model,
                    messages=intent.messages,
                    max_tokens=intent.max_tokens,
                    timeout_ticks=intent.timeout_ticks,
                    tick_duration_seconds=tick_duration_seconds,
                    pricing=self._config.budget.pricing,
                )
            )
        in_flight = self._llm_in_flight_counts(agent_id, task_id)
        return self._budget.check(
            agent_id=agent_id,
            task_id=task_id,
            estimate=estimate,
            in_flight=in_flight,
            agent_concurrency_limit=self._config.max_concurrent_llm_requests,
            # SimulationConfig.budget is the single source of truth for
            # limits (the tracker may hold a stale copy across restores).
            limits=self._config.budget,
        )

    def _audit_budget_rejection(
        self,
        agent_id: str,
        rejection: BudgetCheckResult,
    ) -> None:
        """Audit a whole-round budget rejection (T16c).

        Concurrency keeps the legacy ``permission.denied`` /
        ``llm_budget_exceeded`` shape (back-compat with pre-budget
        tests); cumulative token/cost limits record a dedicated
        ``budget.rejected`` event.
        """
        tick = self._tick_engine.current_tick
        error = f"LLM {rejection.reason}"
        if rejection.dimension == "concurrency":
            self._audit_log.record(
                AuditEventType.PERMISSION_DENIED,
                agent_id=agent_id,
                tick=tick,
                details={
                    "intent": "submit_llm_request",
                    "reason": "llm_budget_exceeded",
                    "error_code": "BUDGET_EXCEEDED",
                    "scope": rejection.scope,
                    "dimension": rejection.dimension,
                    "in_flight": rejection.current,
                    "limit": rejection.limit,
                },
                success=False,
                error=error,
            )
            return
        self._audit_log.record(
            AuditEventType.BUDGET_REJECTED,
            agent_id=agent_id,
            tick=tick,
            details={
                "intent": "submit_llm_request",
                "reason": "budget_exceeded",
                "error_code": "BUDGET_EXCEEDED",
                "scope": rejection.scope,
                "dimension": rejection.dimension,
                "cumulative": rejection.current,
                "estimate": rejection.estimate,
                "limit": rejection.limit,
            },
            success=False,
            error=error,
        )

    def _record_llm_usage(self, op: PendingOperation, tick: int) -> None:
        """Charge one completed LLM invocation to the budget tracker.

        Called when the result is delivered (Phase 1 Ingest). Token
        counts come from the provider's usage report when present;
        otherwise the conservative request estimate is charged so the
        accumulated total never silently under-counts. Wall time is the
        request's in-flight window in simulation time (elapsed ticks ×
        tick duration).
        """
        model = op.metadata.get("model", "") or ""
        tick_duration_seconds = self._tick_engine.config.tick_duration_timedelta.total_seconds()
        wall_time_seconds = max(0, tick - op.created_tick) * tick_duration_seconds
        usage = op.result if isinstance(op.result, dict) else {}
        usage_dict = usage.get("usage") if isinstance(usage, dict) else None
        if isinstance(usage_dict, dict):
            input_tokens = int(usage_dict.get("prompt_tokens", 0) or 0)
            output_tokens = int(usage_dict.get("completion_tokens", 0) or 0)
        else:
            deadline = op.deadline_tick if op.deadline_tick is not None else op.created_tick + 1
            timeout_ticks = max(1, deadline - op.created_tick)
            est = estimate_llm_usage(
                model=model,
                messages=op.metadata.get("messages", []),
                max_tokens=op.metadata.get("max_tokens", 4096),
                timeout_ticks=timeout_ticks,
                tick_duration_seconds=tick_duration_seconds,
                pricing=self._config.budget.pricing,
            )
            input_tokens = est.input_tokens
            output_tokens = est.output_tokens
        self._budget.record_llm(
            agent_id=op.agent_id,
            task_id=op.task_id or "",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            wall_time_seconds=wall_time_seconds,
            pricing=self._config.budget.pricing,
        )

    # -- T4: Journal capture helpers ----------------------------------------

    def _capture_intents(self, plans: dict[str, list[Any]]) -> None:
        """Record intent summaries into the current TickRecord."""
        record = self._journal.current_record
        if record is None:
            return
        for agent_id, intent_list in plans.items():
            for intent in intent_list:
                record.intents.append(
                    IntentSummary(
                        intent_id=getattr(intent, "intent_id", ""),
                        intent_type=getattr(intent, "intent_type", type(intent).__name__),
                        agent_id=agent_id,
                        task_id=getattr(intent, "task_id", ""),
                    )
                )

    def _capture_validation(
        self,
        plans: dict[str, list[Any]],
        validated: dict[str, list[ActionResult]],
    ) -> None:
        """Record validation results into the current TickRecord."""
        record = self._journal.current_record
        if record is None:
            return
        for agent_id, intent_list in plans.items():
            results = validated.get(agent_id, [])
            for i, intent in enumerate(intent_list):
                success = True
                error = None
                if i < len(results):
                    success = results[i].success
                    error = results[i].error
                record.validation.append(
                    IntentSummary(
                        intent_id=getattr(intent, "intent_id", ""),
                        intent_type=getattr(intent, "intent_type", type(intent).__name__),
                        agent_id=agent_id,
                        task_id=getattr(intent, "task_id", ""),
                        success=success,
                        error=error,
                    )
                )

    def _phase_publish(
        self,
        tick: int,
        delivered: list[Email],
        all_results: dict[str, list[ActionResult]],
        ready: list[ReadyCandidate],
    ) -> None:
        """Phase 9: Dispatch pending ops, generate wake events; timeout checks.

        Events generated here are only visible in tick+1.
        """
        # Executor Admission + dispatch of SUBMITTED ops (v0.8.0 P1-4/5)
        self._phase_dispatch(tick)

        # T11: calendar EMIT_EVENT wakes — enqueued here (post-commit)
        # so a rolled-back tick never dispatches (SPEC §3.1: 回滚 tick
        # 不产生 dispatch). CREATE_TASK rules need no wake: the task's
        # assignee sees it via normal task visibility next tick.
        if not self._last_tick_rolled_back:
            for fire in self._calendar_fires_this_tick:
                rule = self._calendar_store.get(fire["rule_id"])
                if rule.action != ScheduleAction.EMIT_EVENT:
                    continue
                runtime_state = self._agent_runtime_states.get(
                    rule.target_agent_id,
                )
                if runtime_state is None:
                    continue
                self._scheduler.enqueue_event(
                    WakeupEvent(
                        event_type=WakeEventType.SCHEDULE_TRIGGER,
                        target_agent_id=rule.target_agent_id,
                        tick=tick,
                        visible_at_tick=tick + 1,
                        source_agent_id="system:calendar",
                        details={"rule_id": rule.rule_id},
                    )
                )
        self._calendar_fires_this_tick = []

        # Timeout checks
        expired_ids = self._timeout_checker.check_task_timeouts(
            self._tick_engine.wall_now(),
            tick,
        )
        self._timeout_checker.check_lock_timeouts(tick)

        # T12a: structured escalation for expired HUMAN tasks (SPEC
        # §10.1 — 不硬编码「通知 Manager → 转人工 → 关闭」阶梯; escalation
        # 结构化 on/mode/target, 一次升级通知 assigner). Post-commit
        # only: a rolled-back tick never escalates.
        if not self._last_tick_rolled_back:
            for tid in expired_ids:
                task = self._task_tree.get(tid)
                assignee_cfg = (
                    self._agent_tree.get(task.assignee_agent_id)
                    if task.assignee_agent_id in self._agent_tree
                    else None
                )
                if assignee_cfg is None or assignee_cfg.kind != "human":
                    continue
                entry = self._outbox.stage(
                    from_agent="system",
                    to=[task.assigner_agent_id],
                    subject=(
                        f"[ESCALATION] Task '{tid}' overdue — human worker "
                        f"'{task.assignee_agent_id}' did not complete on time"
                    ),
                    body=(
                        f"escalation: on=unresolved mode=advise "
                        f"target={task.assigner_agent_id}\n"
                        f"task_id={tid}\n"
                        f"assignee={task.assignee_agent_id}\n"
                        f"deadline={task.deadline.isoformat() if task.deadline else 'none'}"
                    ),
                    email_type="system_notice",
                    task_id=tid,
                    effect_id=f"escalation.{tick}.{tid}",
                    idempotency_key=f"escalation:{tid}:{tick}",
                )
                self._outbox.commit(entry.entry_id)
                self._audit_log.record(
                    AuditEventType.AGENT_FAILED,
                    agent_id=task.assignee_agent_id,
                    tick=tick,
                    details={
                        "task_id": tid,
                        "failure_type": "timeout",
                        "escalation": {
                            "on": "unresolved",
                            "mode": "advise",
                            "target": task.assigner_agent_id,
                        },
                        "assignee": task.assignee_agent_id,
                    },
                    success=False,
                    error=(f"Human task '{tid}' expired — escalated to '{task.assigner_agent_id}'"),
                )

    def _phase_dispatch(self, tick: int) -> None:
        """Executor Admission + dispatch of SUBMITTED tool ops.

        For each SUBMITTED TOOL_REQUEST op:
          1. Admission — an executor must be registered for the tool,
             its tier must match the manifest's execution class, and it
             must have capacity (count-based, includes this op).
          2. TRUSTED_IN_PROCESS executors run the tool synchronously
             (host subprocess bounded by the manifest's max_runtime_ms)
             and complete the op with a structured ToolResultContract;
             out-of-process executors claim the op (→ PENDING) and
             complete it out-of-band.
          3. Denied — the op completes with a structured error so
             Ingest wakes the agent (it decides retry / fail /
             escalate).
        Capacity-full ops stay SUBMITTED and are re-admitted on a later
        tick (backpressure).
        """
        registry = self._pending_ops
        for op in list(registry._operations.values()):
            if op.op_type != OpType.TOOL_REQUEST or op.status != OpStatus.SUBMITTED:
                continue
            if op.tool_request is not None:
                tool_name = op.tool_request.tool_name
                arguments = op.tool_request.arguments
            else:
                tool_name = op.metadata.get("tool_name", "")
                arguments = op.metadata.get("arguments", {})

            # Capacity counts ops CLAIMED by the executor (PENDING);
            # SUBMITTED ops are still queued and charge nothing. The
            # op being admitted is not yet counted.
            in_flight = sum(
                1
                for o in registry._operations.values()
                if o.op_type == OpType.TOOL_REQUEST
                and o.status is OpStatus.PENDING
                and (
                    (o.tool_request.tool_name if o.tool_request is not None else "")
                    or o.metadata.get("tool_name", "")
                )
                == tool_name
            )
            manifest = self._tool_registry.get_manifest(tool_name)
            # T9: provider-level admission for outbound (Integration-owned)
            # tools — an independent gate from executor admission (决策1b).
            # The executor gate governs kernel-side capacity; the provider
            # gate governs the EXTERNAL platform's rate limit. An op is
            # dispatched only when BOTH admit. Provider quota pressure →
            # stay SUBMITTED (backpressure), same as capacity pressure.
            external_tool = op.metadata.get("external_tool", False)
            if external_tool:
                # T12b: a credential_ref declared on the owning Integration
                # must RESOLVE before dispatch. The kernel checks existence
                # only (has() — value-free); the secret itself is fetched
                # at the executor/plugin boundary via resolve(), so the
                # kernel never holds plaintext. Unresolvable ref is a
                # permanent config error (like unknown provider), not
                # backpressure.
                provider = self._integrations.get_by_tool(tool_name)
                if provider is not None and provider.credential_ref:
                    if not self._credential_store.has(
                        provider.credential_ref,
                    ):
                        reason = f"credential_ref '{provider.credential_ref}' is not resolvable"
                        self._audit_log.record(
                            AuditEventType.TOOL_DISPATCHED,
                            agent_id=op.agent_id,
                            tick=tick,
                            details={
                                "request_id": op.request_id,
                                "tool_name": tool_name,
                                "status": "credential_unresolvable",
                                "reason": reason,
                            },
                            success=False,
                            error=reason,
                        )
                        registry.complete(
                            op.request_id,
                            result={
                                "success": False,
                                "error": reason,
                                "error_code": "credential_unresolvable",
                            },
                        )
                        continue
                padm, preason, pretry = self._integrations.admit(tool_name)
                if not padm:
                    if pretry:
                        # rate-limit backpressure: stay SUBMITTED
                        continue
                    # unknown provider → permanent denial
                    self._audit_log.record(
                        AuditEventType.TOOL_DISPATCHED,
                        agent_id=op.agent_id,
                        tick=tick,
                        details={
                            "request_id": op.request_id,
                            "tool_name": tool_name,
                            "status": "provider_denied",
                            "reason": preason,
                        },
                        success=False,
                        error=preason,
                    )
                    registry.complete(
                        op.request_id,
                        result={
                            "success": False,
                            "error": preason,
                            "error_code": "provider_denied",
                        },
                    )
                    continue
            admitted, reason, retryable = self._executors.admit(
                tool_name,
                manifest,
                in_flight,
            )
            if not admitted:
                if retryable:
                    # Capacity pressure: stay SUBMITTED, re-admit next
                    # tick (backpressure) — no error to the agent.
                    continue
                # Permanent denial → structured error to the agent
                self._audit_log.record(
                    AuditEventType.TOOL_DISPATCHED,
                    agent_id=op.agent_id,
                    tick=tick,
                    details={
                        "request_id": op.request_id,
                        "tool_name": tool_name,
                        "status": "admission_denied",
                        "reason": reason,
                    },
                    success=False,
                    error=reason,
                )
                registry.complete(
                    op.request_id,
                    result={
                        "success": False,
                        "error": reason,
                        "error_code": "admission_denied",
                    },
                )
                continue

            tier = self._executors.tier(tool_name)
            if tier in (
                ExecutorTier.TRUSTED_IN_PROCESS,
                ExecutorTier.SANDBOXED_OUT_OF_PROCESS,
            ):
                # In-process executor: run now (manifest-bounded).
                # SANDBOXED_OUT_OF_PROCESS (T16a, run_tests) means the
                # TOOL executes in a sandboxed OS process (rlimits /
                # netns / env sanitisation / temp workspace copy); the
                # kernel-side handler still runs here. request_id lets
                # the handler register its live subprocess for physical
                # cancel (P2-10). File tools read on demand at dispatch
                # (committed state + own staged) — no frozen view is
                # bound (SPEC §3.1 按需化).
                context = ToolContext(
                    agent_id=op.agent_id,
                    tick=tick,
                    request_id=op.request_id,
                )
                tr = self._tool_registry.execute(
                    context=context,
                    tool_name=tool_name,
                    **arguments,
                )
                data = dict(tr.data or {})
                if not tr.success:
                    data.setdefault("success", False)
                    data.setdefault("error", tr.error or "")
                    data.setdefault("error_code", tr.error_code or "")
                registry.complete_tool(
                    op.request_id,
                    ToolResultContract(
                        request_id=op.request_id,
                        status="completed" if tr.success else "failed",
                        data=data,
                        output_hash=hash_payload(data),
                        state_epoch=op.state_epoch,
                    ),
                )
                # A concurrently cancelled op was removed by
                # cancel_operation — the result is fenced; do not
                # record an "executed" audit for it.
                if registry.get_by_id(op.request_id) is None:
                    continue
                self._audit_log.record(
                    AuditEventType.TOOL_DISPATCHED,
                    agent_id=op.agent_id,
                    tick=tick,
                    details={
                        "request_id": op.request_id,
                        "tool_name": tool_name,
                        "status": "executed",
                        "executor_tier": tier.value,
                        "success": tr.success,
                    },
                )
            else:
                # Out-of-process executor: claim the op; the executor
                # completes it out-of-band (e.g. the test harness).
                op.status = OpStatus.PENDING
                # T9: charge the provider's rate-limit window on dispatch
                # (guard so the same op is charged at most once).
                if external_tool and not op.metadata.get("provider_charged"):
                    self._integrations.record_dispatched(tool_name)
                    op.metadata["provider_charged"] = True
                self._audit_log.record(
                    AuditEventType.TOOL_DISPATCHED,
                    agent_id=op.agent_id,
                    tick=tick,
                    details={
                        "request_id": op.request_id,
                        "tool_name": tool_name,
                        "status": "dispatched",
                        "executor_tier": tier.value if tier is not None else "",
                    },
                )

    def _phase_commit(self, tick: int, all_results: dict[str, list[ActionResult]]) -> list[Email]:
        """Phase 8: Commit staged effects atomically.

        1. Validate effects (version, lock, permission, task checks)
        2. Resolve conflicts (deterministic, by agent_id)
        3. Commit all validated effects
        4. Apply committed effects to subsystems
        5. On failure, rollback

        CommitValidate principle: PreValidate (Phase 6) checks "is this
        attempt allowed to try?"; CommitValidate checks "is it still
        committable NOW?" — lock token still valid, KB version still
        matches, task not cancelled/terminal, deadline not passed.
        """
        buffer = self._transaction_buffer
        self._last_tick_rolled_back = False
        self._last_tick_rollback_error = None

        def check_task(effect: StagedEffect) -> str | None:
            """TASK_UPDATE must target an existing, live task.

            Guards the apply path: update_status() would raise on an
            invalid transition (e.g. completing a cancelled/completed
            task) and trigger a FULL-TICK rollback; failing the effect
            here keeps the failure local and deterministic.
            """
            if effect.effect_type != EffectType.TASK_UPDATE:
                return None
            task_id = effect.resource
            if not self._task_tree.exists(task_id):
                return f"Task '{task_id}' not found"
            task = self._task_tree.get(task_id)
            if task.status == TaskStatus.CANCELLED:
                return f"Task '{task_id}' is cancelled"
            if task.is_terminal:
                return f"Task '{task_id}' is already terminal ({task.status.value})"
            now = self._tick_engine.wall_now()
            if task.deadline is not None and task.deadline < now:
                return (
                    f"Task '{task_id}' deadline passed "
                    f"(deadline={task.deadline.isoformat()} < "
                    f"now={now.isoformat()})"
                )
            return None

        def check_version(resource: str, expected: int) -> bool:
            current = self._shared_kb.versions.get_version(resource)
            return current == expected

        def check_lock(
            resource: str,
            agent_id: str,
            lock_token: str | None = None,
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
                    principal=agent_id,
                    path=resource,
                    operation=op,
                )
            return True

        buffer.validate(
            check_version=check_version,
            check_lock=check_lock,
            check_permission=check_permission,
            check_task=check_task,
        )

        # Step 2: Resolve conflicts
        buffer.resolve_conflicts()

        # Step 3: Commit
        committed = buffer.commit()

        # Step 4: Apply committed effects to subsystems.
        # Failure semantics (T18 失败分级, user-approved 2026-08-18):
        #   Deterministic (business) failures — permission/lock/version
        #   in validate; patch-base conflict, duplicate task_id, missing
        #   parent, malformed status at apply — FAIL the effect locally
        #   (EffectStatus.FAILED); the rest of the tick still commits,
        #   NO tick rollback. Group members fail with their group
        #   (already-applied members are individually inverted).
        #   Only an UNEXPECTED exception during apply (kernel failure)
        #   triggers the full-tick rollback, which inverts every applied
        #   effect via its declared invert operation (SPEC §3.3 回滚=逆
        #   操作). No file_previous / kb_state_before dicts — each
        #   effect carries its own prior value in invert_data.
        applied: list[StagedEffect] = []
        failing_effect: StagedEffect | None = None

        def _release_tick_locks() -> None:
            """T20: release every lock acquired this tick (commit end /
            rollback). Idempotent — already-released locks are skipped;
            errors swallowed (the lease is only a backstop)."""
            for resource, agent_id, token in list(self._tick_acquired_locks):
                if not self._lock_manager.is_locked(resource):
                    continue
                try:
                    self._lock_manager.release(resource, agent_id, token)
                    self._audit_log.record(
                        AuditEventType.LOCK_RELEASED,
                        agent_id=agent_id,
                        tick=tick,
                        details={"resource": resource},
                    )
                except Exception:  # noqa: BLE001 — release must not fail
                    pass

        def _invert_one(effect: StagedEffect) -> None:
            """Execute ONE committed effect's declared invert operation
            (SPEC §3.3 / T18). Every EffectType's invert is declared in
            INVERT_CONTRACT and implemented here. Never raises —
            rollback must not fail."""
            kind = INVERT_CONTRACT[effect.effect_type].kind
            data = effect.invert_data
            try:
                if kind == InvertKind.UNREGISTER:  # EMAIL_SEND
                    # Discard the staged (as-yet-undispatched) outbox
                    # entry — the email never happened.
                    entry_id = data.get("outbox_entry_id")
                    if entry_id:
                        self._outbox.rollback_committed(entry_id)
                elif kind == InvertKind.REMOVE_CREATED:  # TASK_CREATE
                    task_id = data.get("task_id", effect.resource)
                    self._task_tree._tasks.pop(task_id, None)
                    parent = self._task_tree._parent_map.pop(task_id, None)
                    self._task_tree._children_map.pop(task_id, None)
                    if parent is not None:
                        # Remove the dangling child edge from the parent's
                        # children list (T16b 矩阵发现：悬空子边随持久化
                        # 留存, 图不一致)。
                        siblings = self._task_tree._children_map.get(parent)
                        if siblings and task_id in siblings:
                            siblings.remove(task_id)
                    for owner, ids in list(self._task_tree._assignee_map.items()):
                        if task_id in ids:
                            ids.remove(task_id)
                elif kind == InvertKind.RESTORE_PREVIOUS:
                    if effect.effect_type in {
                        EffectType.FILE_WRITE,
                        EffectType.FILE_PATCH,
                        EffectType.FILE_DELETE,
                    }:
                        target = Path(data["target_path"])
                        prev = data.get("file_previous")
                        if prev is None:
                            target.unlink(missing_ok=True)
                        elif data.get("is_binary"):
                            target.write_bytes(prev)  # T10 binary restore
                        else:
                            target.write_text(prev, encoding="utf-8")
                    elif effect.effect_type in {
                        EffectType.RECORD_UPSERT,
                        EffectType.RECORD_DELTA,
                    }:
                        # T10: undo the mutation — remove its ledger
                        # entries and restore the prior record (the
                        # ledger stays an exact replay source).
                        self._record_store.invert_mutation(
                            record_type=data["record_type"],
                            key=data["record_key"],
                            prior_record=data.get("record_before"),
                            ledger_ids=data.get("ledger_ids", []),
                        )
                    elif effect.effect_type in {
                        EffectType.KB_WRITE,
                        EffectType.KB_CREATE,
                        EffectType.KB_DELETE,
                    }:
                        res = data.get("kb_resource")
                        ver = data.get("kb_version")
                        if res is None:
                            self._shared_kb._resources.pop(
                                effect.resource,
                                None,
                            )
                        else:
                            self._shared_kb._resources[effect.resource] = res
                        if ver is None:
                            self._shared_kb.versions._versions.pop(
                                effect.resource,
                                None,
                            )
                        else:
                            self._shared_kb.versions._versions[effect.resource] = ver
                    elif effect.effect_type == EffectType.TASK_UPDATE:
                        prior = data.get("task_state_before")
                        if prior is not None:
                            self._task_tree._tasks[effect.resource] = prior
                    elif effect.effect_type == EffectType.RULE_ADVANCE:
                        self._calendar_store.restore(
                            effect.resource,
                            prev_next_run_tick=data.get(
                                "prev_next_run_tick",
                                0,
                            ),
                            prev_last_fired_at=data.get(
                                "prev_last_fired_at",
                            ),
                        )
                    # LOCK_RELEASE / STATE_TRANSITION: declared
                    # RESTORE_PREVIOUS but never staged by the kernel —
                    # nothing to apply.
                elif kind == InvertKind.IRREVERSIBLE:
                    # An in-flight external side effect cannot be undone.
                    # Mark it frankly; the caller records it — never
                    # swallowed silently.
                    effect.error = (
                        f"irreversible effect "
                        f"{effect.effect_type.value} on '{effect.resource}' "
                        "rolled back (compensation required)"
                    )
            except Exception:  # noqa: BLE001 — invert must not fail
                pass

        def _fail_locally(effect: StagedEffect, error: str) -> None:
            """Deterministic (business) failure (T18): FAILED locally,
            NO tick rollback. Group members (atomicity='group') fail
            with their group — already-applied members are individually
            inverted so the group's world-change is fully undone."""
            effect.status = EffectStatus.FAILED
            effect.error = error
            if not (effect.group_id and effect.atomicity == "group"):
                return
            for member in committed:
                if member is effect or member.group_id != effect.group_id:
                    continue
                if member.status == EffectStatus.COMMITTED:
                    _invert_one(member)
                member.status = EffectStatus.FAILED
                member.error = f"group member failed (group {effect.group_id})"

        def _rollback() -> None:
            """Single rollback entry (SPEC §3.3 / T18): release this
            tick's locks, invert every applied effect in REVERSE
            application order, undo this-tick non-effect registrations
            (pending ops, continuations)."""
            # Release this tick's locks first (T20) — a rolled-back
            # tick must not leave a lock held.
            _release_tick_locks()

            # Invert applied effects in reverse application order.
            for effect in reversed(applied):
                _invert_one(effect)

            # P0-2: undo this-tick pending op registrations (and their
            # seen_requests entries — the request_id becomes reusable).
            for _aid, op in self._tick_pending_ops:
                try:
                    self._pending_ops.remove_for_rollback(op.request_id)
                except Exception:  # noqa: BLE001 — rollback must not fail
                    pass

            # P0-2: restore agent continuations to pre-tick state
            for aid, (phase, req_id, req_type) in self._tick_continuations.items():
                try:
                    rs = self._agent_runtime_states.get(aid)
                    if rs:
                        rs.continuation.phase = phase
                        rs.continuation.pending_request_id = req_id
                        rs.continuation.pending_request_type = req_type
                except Exception:  # noqa: BLE001 — rollback must not fail
                    pass

            # Mark applied effects as rolled back
            for ap in applied:
                ap.status = EffectStatus.ROLLED_BACK

        try:
            for effect in committed:
                failing_effect = effect
                if effect.status != EffectStatus.COMMITTED:
                    # Already FAILED — e.g. a group member failed by a
                    # deterministic failure of a sibling. Skip apply.
                    continue

                if effect.effect_type in {
                    EffectType.FILE_WRITE,
                    EffectType.FILE_PATCH,
                }:
                    # Write to private workspace. invert_data captures
                    # file_previous (old content / None) so the single
                    # rollback entry can restore it. FILE_PATCH carries
                    # the full NEW content computed at Act — commit is a
                    # plain write.
                    agent_id = effect.agent_id
                    path = effect.resource
                    content = effect.data.get("content", "")

                    # Path safety: resolve through PrivateStore to catch
                    # traversal (../), symlink escapes, and containment
                    # violations. This is the authoritative gate — earlier
                    # static checks in handle_write / WritePrivateFileIntent
                    # are defense-in-depth.
                    try:
                        target = self._private_store.resolve_path(
                            agent_id,
                            path,
                        )
                    except AccessDeniedError as exc:
                        _fail_locally(effect, f"path denied: {exc}")
                        continue

                    # FILE_PATCH base re-check AT APPLY TIME: earlier
                    # same-tick writes are now visible on disk. If the
                    # content no longer matches the base the patch was
                    # validated against, the patch is stale → local
                    # patch_conflict (mark FAILED, never overwrite).
                    if effect.effect_type == EffectType.FILE_PATCH:
                        base_hash = effect.data.get("base_hash")
                        if base_hash is not None:
                            if target.exists() and target.is_file():
                                current = target.read_text(
                                    encoding="utf-8",
                                )
                            else:
                                current = ""
                            current_hash = hashlib.sha256(
                                current.encode("utf-8"),
                            ).hexdigest()
                            if current_hash != base_hash:
                                _fail_locally(
                                    effect,
                                    f"patch base conflict: file '{path}' "
                                    "changed since Act (content hash "
                                    "mismatch) — re-read and re-apply",
                                )
                                continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    is_binary = bool(
                        effect.data.get(
                            "is_binary",
                            False,
                        )
                    )
                    if is_binary:
                        # T10: binary private-file write — content is
                        # base64 in content_bytes_b64; prior content is
                        # read back as bytes so the invert restores it
                        # byte-exactly.
                        if target.exists() and target.is_file():
                            previous = target.read_bytes()
                        else:
                            previous = None
                        effect.invert_data["target_path"] = str(target)
                        effect.invert_data["file_previous"] = previous
                        effect.invert_data["is_binary"] = True
                        try:
                            payload = base64.b64decode(
                                effect.data.get("content_bytes_b64", ""),
                            )
                        except Exception:  # noqa: BLE001 — malformed b64
                            _fail_locally(
                                effect,
                                "invalid base64 content_bytes_b64",
                            )
                            continue
                        target.write_bytes(payload)
                    else:
                        if target.exists():
                            prev_text: str | None = target.read_text(
                                encoding="utf-8",
                            )
                        else:
                            prev_text = None
                        effect.invert_data["target_path"] = str(target)
                        effect.invert_data["file_previous"] = prev_text
                        target.write_text(content, encoding="utf-8")
                    # The on-disk invert RESTORES text via read_text; for
                    # a binary prior this must restore bytes. Handled by
                    # the is_binary flag in _invert_one below.

                elif effect.effect_type == EffectType.EMAIL_SEND:
                    data = effect.data
                    # Stage + commit in the outbox. Dispatch is a single
                    # post-commit loop (below) — it must run even on
                    # ticks with no new email effect so that leftover
                    # COMMITTED entries (restart continuation, retry
                    # backoff) are still delivered.
                    entry = self._outbox.stage(
                        from_agent=data.get("from_agent", effect.agent_id),
                        to=data.get("to", []),
                        subject=data.get("subject", ""),
                        body=data.get("body", ""),
                        email_type=data.get("email_type", "progress"),
                        task_id=data.get("task_id", ""),
                        effect_id=effect.effect_id,
                        attachments=data.get("attachments", []),
                    )
                    self._outbox.commit(entry.entry_id)
                    # invert_data: the entry to discard if this effect
                    # ever needs inverting (rollback / group failure).
                    effect.invert_data["outbox_entry_id"] = entry.entry_id

                elif effect.effect_type == EffectType.TASK_CREATE:
                    data = effect.data
                    task_id = data.get("task_id", effect.resource)
                    # Deterministic pre-checks (T18 失败分级): duplicate
                    # task_id and missing parent are BUSINESS failures —
                    # FAILED locally, NO tick rollback.
                    if self._task_tree.exists(task_id):
                        _fail_locally(
                            effect,
                            f"Task '{task_id}' already exists",
                        )
                        continue
                    derived_from = data.get("derived_from")
                    if derived_from is not None and not self._task_tree.exists(derived_from):
                        _fail_locally(
                            effect,
                            f"Derived-from task '{derived_from}' not found",
                        )
                        continue
                    effect.invert_data["task_id"] = task_id
                    try:
                        priority = TaskPriority(
                            data.get("priority", TaskPriority.NORMAL.value),
                        )
                    except ValueError:
                        priority = TaskPriority.NORMAL
                    self._task_tree.create(
                        task_id=task_id,
                        title=data.get("title", ""),
                        description=data.get("description", ""),
                        assigner_agent_id=data.get(
                            "assigner_agent_id",
                            effect.agent_id,
                        ),
                        assignee_agent_id=data.get("assignee_agent_id", ""),
                        derived_from=derived_from,
                        priority=priority,
                        deadline=data.get("deadline"),
                        status=TaskStatus.ASSIGNED,
                        tick=tick,
                    )

                elif effect.effect_type == EffectType.RULE_ADVANCE:
                    # T11 决策 1: schedule-state advancement is part of
                    # the tick transaction — invert_data captures the
                    # prior state for the single rollback entry.
                    data = effect.data
                    rule_id = data["rule_id"]
                    if not self._calendar_store.exists(rule_id):
                        _fail_locally(
                            effect,
                            f"Schedule rule '{rule_id}' not found",
                        )
                        continue
                    rule = self._calendar_store.get(rule_id)
                    effect.invert_data["prev_next_run_tick"] = rule.next_run_tick
                    effect.invert_data["prev_last_fired_at"] = rule.last_fired_at
                    self._calendar_store.advance(
                        rule_id,
                        next_run_tick=data.get("next_run_tick"),
                        last_fired_at=data.get("last_fired_at"),
                    )

                elif effect.effect_type == EffectType.TASK_UPDATE:
                    data = effect.data
                    task_id = effect.resource
                    # Deterministic guards (T18 失败分级): malformed
                    # status strings and unreachable transitions are
                    # BUSINESS failures, not kernel rollbacks.
                    try:
                        new_status = TaskStatus(
                            data.get("status", "in_progress"),
                        )
                    except ValueError:
                        _fail_locally(
                            effect,
                            f"Invalid task status: {data.get('status')!r}",
                        )
                        continue
                    prior = self._task_tree.get(task_id)
                    effect.invert_data["task_state_before"] = prior.model_copy(deep=True)
                    try:
                        self._task_tree.update_status(
                            task_id,
                            new_status,
                            tick=tick,
                            allow_walk=True,
                        )
                    except InvalidTransitionError as exc:
                        _fail_locally(effect, str(exc))
                        continue
                    task = self._task_tree.get(task_id)
                    if data.get("summary"):
                        task.metadata["summary"] = data["summary"]
                    if data.get("artifacts"):
                        task.metadata["artifacts"] = data["artifacts"]
                    if data.get("reason"):
                        task.metadata["reason"] = data["reason"]

                elif effect.effect_type in {
                    EffectType.KB_WRITE,
                    EffectType.KB_CREATE,
                    EffectType.KB_DELETE,
                }:
                    # Apply shared KB write via the internal commit path
                    # (permission/lock/version already validated in
                    # Phase 6-8). invert_data captures prior resource +
                    # version so the rollback entry can restore them.
                    data = effect.data
                    path = effect.resource
                    # Snapshot prior KB state for this effect (content +
                    # version) BEFORE mutating.
                    res = self._shared_kb._resources.get(path)
                    ver = self._shared_kb.versions.get_info(path)
                    effect.invert_data["kb_resource"] = (
                        res.model_copy(deep=True) if res is not None else None
                    )
                    effect.invert_data["kb_version"] = (
                        ver.model_copy(deep=True) if ver is not None else None
                    )
                    if effect.effect_type == EffectType.KB_WRITE:
                        self._shared_kb._apply_committed(
                            path=path,
                            agent_id=effect.agent_id,
                            content=data.get("content", ""),
                            expected_version=data.get("expected_version", 0),
                            tick=tick,
                        )
                    elif effect.effect_type == EffectType.KB_CREATE:
                        self._shared_kb.create(
                            path=path,
                            agent_id=effect.agent_id,
                            content=data.get("content", ""),
                            tick=tick,
                        )
                    elif effect.effect_type == EffectType.KB_DELETE:
                        self._shared_kb.delete(
                            path=path,
                            agent_id=effect.agent_id,
                        )

                elif effect.effect_type in {
                    EffectType.RECORD_UPSERT,
                    EffectType.RECORD_DELTA,
                }:
                    # T10: typed record mutation via the store (invariant
                    # checks inside). An invariant violation is a
                    # DETERMINISTIC business failure (T18) — the store
                    # raises RecordInvariantError → _fail_locally, never
                    # a tick rollback. invert_data captures the prior
                    # record + appended ledger ids for per-effect undo.
                    data = effect.data
                    record_type = data.get("record_type", "")
                    key = str(data.get("key", effect.resource))
                    agent_id = effect.agent_id
                    prior_record = self._record_store.get(record_type, key)
                    effect.invert_data["record_type"] = record_type
                    effect.invert_data["record_key"] = key
                    effect.invert_data["record_before"] = (
                        dict(prior_record) if prior_record is not None else None
                    )
                    try:
                        if effect.effect_type == EffectType.RECORD_UPSERT:
                            result = self._record_store.upsert(
                                record_type=record_type,
                                key=key,
                                data=data.get("record", {}),
                                agent_id=agent_id,
                                tick=tick,
                            )
                        else:
                            result = self._record_store.apply_delta(
                                record_type=record_type,
                                key=key,
                                field=data.get("field", ""),
                                delta=float(data.get("delta", 0)),
                                agent_id=agent_id,
                                tick=tick,
                            )
                    except RecordInvariantError as exc:
                        _fail_locally(effect, exc.message)
                        continue
                    effect.invert_data["ledger_ids"] = result.ledger_ids

                applied.append(effect)
        except Exception as e:  # noqa: BLE001 — kernel failure → full rollback
            # Only an UNEXPECTED apply exception reaches here (T18):
            # deterministic failures were handled above via
            # _fail_locally (no raise). Full-tick rollback inverts every
            # applied effect via its declared invert operation.
            self._last_tick_rolled_back = True
            self._last_tick_rollback_error = str(e)
            _rollback()
            # Any committed effect not applied is marked ROLLED_BACK;
            # the failing effect carries the error.
            for eff in committed:
                if eff.status == EffectStatus.COMMITTED:
                    eff.status = EffectStatus.ROLLED_BACK
            if failing_effect is not None:
                failing_effect.status = EffectStatus.FAILED
                failing_effect.error = str(e)
            # Rollback invalidates the state the tick was computed
            # against: bump the state epoch so in-flight external
            # results from the old epoch are fenced as stale.
            self._bump_state_epoch()
            # T19: a kernel-level rollback is a CRASH event for the
            # crash guard (repeated crashes → emergency callbacks +
            # auto-pause). Deterministic failures never reach here.
            self._crash_guard.record_crash(
                tick,
                str(e),
                self._state_epoch,
            )
            self._audit_log.record(
                AuditEventType.TRANSACTION_ROLLBACK,
                tick=tick,
                details={
                    "error": str(e),
                    "rolled_back": [ap.effect_id for ap in applied],
                    "new_state_epoch": self._state_epoch,
                },
                success=False,
                error=str(e),
            )
            # T4: record rolled-back effects in journal
            record = self._journal.current_record
            if record is not None:
                for effect in committed:
                    record.effects.append(
                        EffectSummary(
                            effect_id=effect.effect_id,
                            effect_type=effect.effect_type.value,
                            agent_id=effect.agent_id,
                            resource=effect.resource,
                            status=effect.status.value,
                            error=effect.error,
                        )
                    )
            return []

        # T20: every lock acquired this tick is released at commit end
        # (写事务提交即释). Covers success, deterministic failures in
        # validate/apply, and group failures — the lock never survives
        # the tick; the lease is only a backstop.
        _release_tick_locks()

        # Outbox dispatch runs unconditionally after a successful
        # commit: entries committed THIS tick plus leftover COMMITTED
        # entries (restart continuation, retry backoff) are delivered
        # here. Emails are created only after the full commit
        # succeeded — a rolled-back tick never creates emails.
        from my_team.models.email import EmailType

        def _deliver(entry: Any) -> None:
            self._mail_system.create_email(
                from_agent=entry.from_agent,
                to=entry.to,
                subject=entry.subject,
                body=entry.body,
                email_type=EmailType(entry.email_type),
                tick=tick,
                deliver_at_tick=tick + self._config.email_delivery_latency_ticks,
                task_id=entry.task_id,
                attachments=list(entry.attachments),
            )

        self._outbox.dispatch(_deliver, current_tick=tick)

        # Record audit for committed effects (apply-time failures such
        # as a stale FILE_PATCH are marked FAILED — not audited as
        # commits)
        for effect in committed:
            if effect.status != EffectStatus.COMMITTED:
                continue
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

        # T4: record effects, pending ops, outbox in journal
        record = self._journal.current_record
        if record is not None:
            for effect in committed:
                record.effects.append(
                    EffectSummary(
                        effect_id=effect.effect_id,
                        effect_type=effect.effect_type.value,
                        agent_id=effect.agent_id,
                        resource=effect.resource,
                        status=effect.status.value,
                        error=effect.error,
                    )
                )
            for _aid, op in self._tick_pending_ops:
                record.pending_ops.append(
                    PendingOpSummary(
                        request_id=op.request_id,
                        op_type=op.op_type.value,
                        agent_id=op.agent_id,
                        created_tick=op.created_tick,
                    )
                )
            # Capture outbox entries created this tick
            for entry in self._outbox.entries_by_status(OutboxStatus.COMMITTED):
                if entry.effect_id and any(e.effect_id == entry.effect_id for e in committed):
                    record.outbox.append(
                        OutboxSummary(
                            entry_id=entry.entry_id,
                            effect_id=entry.effect_id,
                            from_agent=entry.from_agent,
                            to=entry.to,
                            subject=entry.subject,
                        )
                    )

        return []

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
