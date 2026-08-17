"""Agent runtime interface: ToolContext, AgentRuntime protocol, and tool registry.

Per SPEC §8.2 (Phases 3-5), §10, §15.1:
- ToolContext binds agent identity to every tool call
- AgentRuntime defines observe/decide/act protocol
- Tool registry enforces per-agent tool permissions
- Root Agent restricted to read/write/ls/delegate only

v0.6.0: decide() produces list[Intent] — finite, non-blocking steps in
the agent's ReAct continuation. ActionPlan remains as the internal
parsing result of LLM responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from my_team.models.intent import (
    CompleteTaskIntent,
    DelegateIntent,
    FailTaskIntent,
    Intent,
    SendEmailIntent,
    SubmitToolRequest,
    WritePrivateFileIntent,
)
from my_team.tool_manifest import (
    OperationPolicy,
    PolicyDecision,
    ToolManifest,
)

# ---------------------------------------------------------------------------
# ToolContext — identity binding for every tool call
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolContext:
    """Immutable context绑定到每次工具调用。

    系统侧从 context 获取 agent_id，忽略调用者提交的身份字段。
    这是防止身份伪造的核心机制 (SPEC §15.1, §18.10)。
    """

    agent_id: str
    simulation_id: str = ""
    tick: int = 0
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    # Frozen per-agent file view captured at Freeze (v0.6.0 hardening):
    # {"files": {relpath: content}, "dirs": [relpaths]}. Read-only tools
    # execute against this view instead of the live filesystem.
    read_view: dict[str, Any] | None = field(default=None)


class ToolPermissionError(Exception):
    """Raised when an agent attempts to use a tool it doesn't have."""

    def __init__(self, agent_id: str, tool_name: str) -> None:
        self.agent_id = agent_id
        self.tool_name = tool_name
        super().__init__(
            f"Agent '{agent_id}' does not have permission to use tool '{tool_name}'"
        )


class ToolResult(BaseModel):
    """Standardized result from a tool execution."""

    success: bool = Field(description="Whether the tool call succeeded")
    data: Any = Field(default=None, description="Tool output data")
    error: str | None = Field(default=None, description="Error message if failed")
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code: permission_denied, tool_error, not_found, etc.",
    )
    retryable: bool = Field(
        default=True,
        description="Whether this failure is retryable (permission_denied is not)",
    )
    agent_id: str = Field(default="", description="Agent that called the tool")
    tool_name: str = Field(default="", description="Tool that was called")
    tick: int = Field(default=0, description="Tick at which tool was called")


# ---------------------------------------------------------------------------
# Tool registry — enforces per-agent tool permissions
# ---------------------------------------------------------------------------

class ToolCategory(str, Enum):
    """Categories of tools for permission grouping."""

    FILE_OPS = "file_ops"           # read, write, ls on private space
    SHARED_KB = "shared_kb"         # read, write on shared knowledge base
    EMAIL = "email"                 # send_email
    DELEGATE = "delegate"           # delegate to children
    SYSTEM = "system"               # system-level operations


# Default tool sets per SPEC §4.2, §4.3
ROOT_TOOLS = frozenset({"read", "write", "ls", "delegate"})
MANAGER_TOOLS = frozenset({"read", "write", "ls", "delegate", "send_email"})
WORKER_TOOLS = frozenset({"read", "write", "ls", "send_email"})


class ToolRegistry:
    """Central registry that validates tool calls against agent permissions.

    Every tool call must go through this registry. The system provides
    the ToolContext (with agent_id), and the registry verifies the agent
    has permission to use the requested tool.

    v0.7.0: tools are registered with a ToolManifest (declarative
    contract — see tool_manifest.py). Registration validates the
    manifest. An OperationPolicy (deny-by-default) may be attached:
    execution then requires (a) the agent's tool permission AND (b) a
    policy decision allowing the tool. Bare handler registration
    (without manifest) remains supported for legacy callers; policy
    enforcement requires manifests for all policy-checked tools.
    """

    def __init__(self) -> None:
        self._agent_tools: dict[str, frozenset[str]] = {}
        self._tool_handlers: dict[str, Any] = {}
        self._manifests: dict[str, ToolManifest] = {}
        self._policy: OperationPolicy | None = None

    def register_agent(self, agent_id: str, tools: frozenset[str]) -> None:
        """Register the allowed tools for an agent."""
        self._agent_tools[agent_id] = tools

    def register_manifest(self, manifest: ToolManifest) -> None:
        """Register a tool manifest (registration-time validation).

        Raises ToolManifestError if the manifest is invalid.
        """
        self._manifests[manifest.name] = manifest

    def register_handler(
        self,
        tool_name: str,
        handler: Any,
        manifest: ToolManifest | None = None,
    ) -> None:
        """Register a callable handler for a tool, optionally with its
        manifest (validated at registration)."""
        if manifest is not None:
            if manifest.name != tool_name:
                raise ValueError(
                    f"Manifest name '{manifest.name}' does not match "
                    f"tool '{tool_name}'"
                )
            self.register_manifest(manifest)
        self._tool_handlers[tool_name] = handler

    def get_manifest(self, tool_name: str) -> ToolManifest | None:
        """Get the manifest for a tool (None if not registered)."""
        return self._manifests.get(tool_name)

    def manifests(self) -> tuple[ToolManifest, ...]:
        """All registered manifests, sorted by tool name."""
        return tuple(
            self._manifests[name] for name in sorted(self._manifests)
        )

    def set_policy(self, policy: OperationPolicy) -> None:
        """Attach a deployment policy (deny-by-default)."""
        self._policy = policy

    @property
    def policy(self) -> OperationPolicy | None:
        return self._policy

    def policy_decision(self, tool_name: str) -> PolicyDecision:
        """Check a tool against the attached policy.

        Without a policy: always allowed. With a policy: a tool without
        a manifest cannot be policy-checked → denied (deny-by-default).
        """
        if self._policy is None:
            return PolicyDecision(True, False, "")
        manifest = self._manifests.get(tool_name)
        if manifest is None:
            return PolicyDecision(
                False, False,
                f"Tool '{tool_name}' has no manifest; cannot policy-check",
            )
        return self._policy.decide(manifest)

    def authorize(self, context: ToolContext, tool_name: str) -> None:
        """Verify the agent has permission to use the tool.

        Raises ToolPermissionError if not authorized.
        """
        allowed = self._agent_tools.get(context.agent_id, frozenset())
        if tool_name not in allowed:
            raise ToolPermissionError(context.agent_id, tool_name)

    def can_use(self, agent_id: str, tool_name: str) -> bool:
        """Check if an agent can use a tool (non-raising)."""
        allowed = self._agent_tools.get(agent_id, frozenset())
        return tool_name in allowed

    def get_allowed_tools(self, agent_id: str) -> frozenset[str]:
        """Get the set of tools an agent is allowed to use."""
        return self._agent_tools.get(agent_id, frozenset())

    def execute(
        self,
        context: ToolContext,
        tool_name: str,
        **kwargs: Any,
    ) -> ToolResult:
        """Authorize and execute a tool call.

        1. Verify agent has permission
        2. Look up handler
        3. Execute with context
        """
        try:
            self.authorize(context, tool_name)
        except ToolPermissionError as e:
            return ToolResult(
                success=False,
                error=str(e),
                error_code="permission_denied",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )

        # Policy gate (v0.7.0): deny-by-default when a policy is attached.
        decision = self.policy_decision(tool_name)
        if not decision.allowed:
            return ToolResult(
                success=False,
                error=decision.reason,
                error_code="policy_denied",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )
        if decision.requires_approval:
            return ToolResult(
                success=False,
                error=decision.reason,
                error_code="requires_approval",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )

        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"No handler registered for tool '{tool_name}'",
                error_code="not_found",
                retryable=False,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )

        try:
            result = handler(context=context, **kwargs)
            if isinstance(result, ToolResult):
                return result
            return ToolResult(
                success=True,
                data=result,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                error_code="tool_error",
                retryable=True,
                agent_id=context.agent_id,
                tool_name=tool_name,
                tick=context.tick,
            )


# ---------------------------------------------------------------------------
# Agent observations and action plans
# ---------------------------------------------------------------------------

class AgentObservation(BaseModel):
    """What an agent sees during the Observe phase (SPEC §8.2 Phase 3)."""

    agent_id: str = Field(description="Agent receiving this observation")
    tick: int = Field(description="Current simulation tick")
    emails: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Emails delivered to this agent's inbox",
    )
    task_states: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="States of tasks owned by this agent",
    )
    shared_kb_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Relevant shared KB state",
    )
    lock_states: dict[str, Any] = Field(
        default_factory=dict,
        description="Lock states relevant to this agent",
    )
    private_workspace_path: str = Field(
        default="",
        description="Path to agent's private workspace",
    )


def _proxy(d: dict[str, Any]) -> MappingProxyType[str, Any]:
    """Wrap a dict in MappingProxyType for deep immutability."""
    return MappingProxyType(d)


def _proxy_nested(
    d: dict[str, dict[str, Any]],
) -> MappingProxyType[str, MappingProxyType[str, Any]]:
    """Wrap a nested dict in MappingProxyType for deep immutability."""
    return MappingProxyType({k: MappingProxyType(v) for k, v in d.items()})


@dataclass(frozen=True)
class AgentSnapshot:
    """Immutable per-agent view of the simulation state at tick boundary.

    Constructed by the simulation during Phase 3 (Observe) and passed
    to each agent's observe() method. Agents should read from this
    snapshot and produce an AgentObservation.

    All fields are deeply immutable:
    - Frozen dataclass prevents field reassignment
    - emails is a tuple of dicts (tuple is immutable)
    - task_states, shared_kb_snapshot, lock_states use MappingProxyType
      to prevent mutation of nested values
    """

    tick: int = 0
    emails: tuple[dict[str, Any], ...] = ()
    task_states: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    shared_kb_snapshot: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    lock_states: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    private_workspace_path: str = ""

    def __post_init__(self) -> None:
        """Wrap mutable fields in MappingProxyType for deep immutability."""
        # Frozen dataclass allows mutation in __post_init__ via object.__setattr__
        if not isinstance(self.task_states, MappingProxyType):
            object.__setattr__(
                self, "task_states",
                MappingProxyType({k: MappingProxyType(v) for k, v in self.task_states.items()})
            )
        if not isinstance(self.shared_kb_snapshot, MappingProxyType):
            object.__setattr__(
                self, "shared_kb_snapshot", MappingProxyType(self.shared_kb_snapshot)
            )
        if not isinstance(self.lock_states, MappingProxyType):
            object.__setattr__(
                self, "lock_states", MappingProxyType(self.lock_states)
            )


class AgentAction(BaseModel):
    """A single action produced by an agent during Decide (SPEC §8.2 Phase 4)."""

    action_type: str = Field(description="Action type: send_email, read, write, delegate, etc.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Action parameters")
    tool_name: str = Field(default="", description="Tool to use for this action")


class ActionPlan(BaseModel):
    """An agent's planned actions for the current tick."""

    agent_id: str = Field(description="Agent that produced this plan")
    tick: int = Field(description="Tick this plan is for")
    actions: list[AgentAction] = Field(default_factory=list, description="Planned actions")


class ActionResult(BaseModel):
    """Result of executing a single action."""

    action: AgentAction = Field(description="The action that was executed")
    success: bool = Field(description="Whether the action succeeded")
    result_data: Any = Field(default=None, description="Action output")
    error: str | None = Field(default=None, description="Error if failed")


def action_plan_to_intents(plan: ActionPlan) -> list[Intent]:
    """Convert a legacy ActionPlan into a list of Intents.

    This bridges the old rule-based agent interface (which produces
    AgentAction lists) to the v0.6.0 Intent model. Rule-based agents
    (BaseAgent subclasses) can keep producing ActionPlans; the system
    converts them to Intents before staging.

    Mapping:
      - delegate   → DelegateIntent
      - send_email → SendEmailIntent
      - write      → WritePrivateFileIntent
      - read/ls    → SubmitToolRequest
    """
    intents: list[Intent] = []
    for action in plan.actions:
        payload = action.payload or {}
        if action.action_type == "delegate":
            intents.append(DelegateIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                recipient_agent_id=payload.get("recipient_agent_id", ""),
                task_title=payload.get("task_title", ""),
                task_description=payload.get("task_description", ""),
                parent_task_id=payload.get("parent_task_id", ""),
                deadline_tick=payload.get("deadline_tick"),
            ))
        elif action.action_type == "send_email":
            intents.append(SendEmailIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                to=list(payload.get("to", [])),
                subject=payload.get("subject", ""),
                body=payload.get("body", ""),
                email_type=payload.get("email_type", "progress"),
            ))
        elif action.action_type == "write":
            intents.append(WritePrivateFileIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                path=payload.get("path", ""),
                content=payload.get("content", ""),
            ))
        elif action.action_type in {"read", "ls", "kb_write", "kb_create"}:
            intents.append(SubmitToolRequest(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                tool_name=action.tool_name or action.action_type,
                arguments=payload,
            ))
        elif action.action_type == "complete_task":
            intents.append(CompleteTaskIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                summary=payload.get("summary", ""),
                artifacts=payload.get("artifacts", []),
            ))
        elif action.action_type == "fail_task":
            intents.append(FailTaskIntent(
                agent_id=plan.agent_id,
                task_id=payload.get("task_id", ""),
                reason=payload.get("reason", ""),
                retryable=payload.get("retryable", False),
            ))
    return intents


class ActionContext(BaseModel):
    """Context for action execution (SPEC §8.2 Phase 5)."""

    agent_id: str
    tick: int
    tool_context: ToolContext = Field(description="Identity-bound context for tool calls")


# ---------------------------------------------------------------------------
# AgentRuntime protocol — what a real agent implementation must provide
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentRuntime(Protocol):
    """Protocol that all agent implementations must satisfy.

    This defines the three-phase interface for agent execution within
    a simulation tick:

    1. observe: Read the frozen snapshot and produce observations
    2. decide: Generate an action plan based on observations
    3. act: Execute the planned actions
    """

    @property
    def agent_id(self) -> str:
        """Unique identifier for this agent."""
        ...

    def observe(self, snapshot: AgentSnapshot) -> AgentObservation:
        """Phase 3 (Observe): Read the frozen snapshot.

        The agent reads:
        - New emails in inbox
        - Current task states
        - Private workspace state
        - Shared KB snapshot
        - Lock states
        - System notifications

        Returns an AgentObservation with relevant data.
        """
        ...

    def decide(self, observation: AgentObservation) -> ActionPlan:
        """Phase 4 (Decide): Generate an action plan (legacy interface).

        Rule-based agents produce an ActionPlan. The system converts it
        to Intents via action_plan_to_intents(). LLM agents should
        override decide_intents() instead — this method is retained for
        backward compatibility with rule-based agents.
        """
        ...

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: Any = None,
    ) -> list[Intent]:
        """Phase 5 (Decide, v0.6.0): Produce non-blocking Intents.

        The agent analyzes its observations and produces 0 or more
        Intents — finite, non-blocking steps in its ReAct continuation.
        No side effects occur during this phase.

        Intents can be:
          - SubmitLLMRequest  (async LLM call, does NOT block)
          - SubmitToolRequest (async tool call)
          - SendEmailIntent / DelegateIntent / WritePrivateFileIntent
          - WaitForEventIntent / CompleteTaskIntent / FailTaskIntent

        The default implementation converts decide()'s ActionPlan.
        """
        ...

    def act(
        self,
        plan: ActionPlan,
        context: ActionContext,
    ) -> list[ActionResult]:
        """Phase 5 (Act): Execute the planned actions.

        Actions produce staged effects that are not visible to other
        agents until the Commit phase.

        Each tool call must go through the ToolRegistry with the
        agent's ToolContext for identity verification.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in agent implementations
# ---------------------------------------------------------------------------

class BaseAgent:
    """Base implementation of AgentRuntime with common functionality.

    Subclass this and override decide() to implement custom agent logic.
    """

    def __init__(
        self,
        agent_id: str,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._tool_registry = tool_registry or ToolRegistry()
        self._tool_context = ToolContext(
            agent_id=agent_id,
            allowed_tools=self._tool_registry.get_allowed_tools(agent_id),
        )

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def tool_context(self) -> ToolContext:
        return self._tool_context

    def observe(self, snapshot: AgentSnapshot) -> AgentObservation:
        """Default observe: extract relevant data from snapshot."""
        # Convert MappingProxyType back to plain dicts for AgentObservation
        task_states = {
            k: dict(v) for k, v in snapshot.task_states.items()
        }
        return AgentObservation(
            agent_id=self._agent_id,
            tick=snapshot.tick,
            emails=list(snapshot.emails),
            task_states=task_states,
            shared_kb_snapshot=dict(snapshot.shared_kb_snapshot),
            lock_states=dict(snapshot.lock_states),
            private_workspace_path=snapshot.private_workspace_path,
        )

    def decide(self, observation: AgentObservation) -> ActionPlan:
        """Default decide: no actions (override in subclass)."""
        return ActionPlan(
            agent_id=self._agent_id,
            tick=observation.tick,
            actions=[],
        )

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: Any = None,
    ) -> list[Intent]:
        """Default decide_intents: convert decide()'s ActionPlan to Intents.

        Rule-based agents keep overriding decide() to produce
        ActionPlans; the system converts them to Intents automatically.
        LLM agents override this method directly to produce
        SubmitLLMRequest intents.
        """
        plan = self.decide(observation)
        return action_plan_to_intents(plan)

    def act(
        self,
        plan: ActionPlan,
        context: ActionContext,
    ) -> list[ActionResult]:
        """Default act: execute actions through tool registry."""
        results: list[ActionResult] = []
        for action in plan.actions:
            try:
                tool_context = ToolContext(
                    agent_id=self._agent_id,
                    tick=context.tick,
                    allowed_tools=self._tool_context.allowed_tools,
                )
                result = self._tool_registry.execute(
                    context=tool_context,
                    tool_name=action.tool_name,
                    **action.payload,
                )
                results.append(ActionResult(
                    action=action,
                    success=result.success,
                    result_data=result.data,
                    error=result.error,
                ))
            except ToolPermissionError as e:
                results.append(ActionResult(
                    action=action,
                    success=False,
                    error=str(e),
                ))
        return results


class RootAgent(BaseAgent):
    """Root decision agent (SPEC §4.2).

    Constraints:
    - Tools: read, write, ls, delegate ONLY
    - Cannot execute business tools
    - Cannot directly modify sub-agent state
    - Cannot bypass email to modify sub-agents
    """

    def __init__(self, agent_id: str = "agent.root", **kwargs: Any) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        # Enforce Root Agent tool restriction
        self._tool_context = ToolContext(
            agent_id=agent_id,
            allowed_tools=ROOT_TOOLS,
        )


class SubAgent(BaseAgent):
    """Sub-agent with role-specific tools (SPEC §4.3).

    Sub-agents can have business-specific tools (web_search, etc.)
    in addition to base tools.
    """

    def __init__(
        self,
        agent_id: str,
        extra_tools: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> None:
        tools = WORKER_TOOLS
        if extra_tools:
            tools = tools | extra_tools
        super().__init__(agent_id=agent_id, **kwargs)
        self._tool_context = ToolContext(
            agent_id=agent_id,
            allowed_tools=tools,
        )


class ManagerAgent(BaseAgent):
    """Manager agent that can delegate (e.g., Research Agent).

    Has delegate + send_email in addition to base tools.
    """

    def __init__(
        self,
        agent_id: str,
        extra_tools: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> None:
        tools = MANAGER_TOOLS
        if extra_tools:
            tools = tools | extra_tools
        super().__init__(agent_id=agent_id, **kwargs)
        self._tool_context = ToolContext(
            agent_id=agent_id,
            allowed_tools=tools,
        )
