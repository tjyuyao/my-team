"""Agent data model.

Defines the core Agent entity and its configuration schema,
per SPEC §4.1, §4.2, §4.3.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AgentRole(str, Enum):
    """Predefined agent roles."""

    ROOT_DECISION = "root_decision_agent"
    RESEARCH_MANAGER = "research_manager"
    WEB_RESEARCHER = "web_researcher"
    DATA_ANALYST = "data_analyst"
    PLANNING = "planning_manager"
    REVIEW = "review_manager"
    QUALITY_CHECK = "quality_check_agent"
    CUSTOM = "custom"


class AgentStatus(str, Enum):
    """Agent runtime states per SPEC §9."""

    CREATED = "created"
    INITIALIZED = "initialized"
    READY = "ready"
    # Running sub-states
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING = "waiting"
    BLOCKED = "blocked"
    PAUSED = "paused"
    FAILED = "failed"
    TERMINATED = "terminated"


class AgentTool(str, Enum):
    """Available agent tools."""

    READ = "read"
    WRITE = "write"
    LS = "ls"
    DELEGATE = "delegate"
    SEND_EMAIL = "send_email"
    WEB_SEARCH = "web_search"


class SharedKBPermission(BaseModel):
    """Permission rule for shared knowledge base access, per SPEC §6.2."""

    scope: str = Field(description="Path pattern, e.g. 'project/research/*'")
    principal: str = Field(description="Agent ID this rule applies to")
    allow: list[str] = Field(
        description="Allowed operations: list, read, create, write, append, "
        "rename, delete, lock, unlock, publish"
    )


class PoolMode(str, Enum):
    """WorkerPool routing behavior (T11 决策 3, SPEC §9.3)."""

    IMMEDIATE = "immediate"  # delegate → select child → copy same tick
    DEFERRED = "deferred"    # queue at manager; dispatch when child idle


class PoolStrategy(str, Enum):
    """Declarative child-selection rules (round_robin needs no LLM)."""

    ROUND_ROBIN = "round_robin"
    LEAST_BUSY = "least_busy"
    SKILL_MATCH = "skill_match"


class PoolConfig(BaseModel):
    """WorkerPool behavior on a ``kind=service`` manager (SPEC §9.3).

    pool = service manager + children + declarative routing rules —
    no independent pool primitive, no pool_id. Selection is executed
    by the kernel as the manager's rule-driven behavior.
    """

    mode: PoolMode = PoolMode.IMMEDIATE
    strategy: PoolStrategy = PoolStrategy.LEAST_BUSY


class AgentConfig(BaseModel):
    """Agent configuration as loaded from JSON, per SPEC §17.

    This is the *static* definition of an agent. Runtime state
    (status, current task, memory) is managed separately.
    """

    agent_id: str = Field(description="Unique identifier, e.g. 'agent.research'")
    display_name: str = Field(description="Human-readable name")
    role: str = Field(description="Agent role identifier")
    kind: Literal["llm", "human", "service"] = Field(
        default="llm",
        description=(
            "Agent kind (SPEC §4.1): llm = LLM-driven; human = UI-queue "
            "driven; service = external-service proxy / rule-driven "
            "(WorkerPool manager, §9.3)"
        ),
    )
    pool: PoolConfig | None = Field(
        default=None,
        description=(
            "WorkerPool routing config; only valid for kind=service "
            "managers with children (T11 决策 3)"
        ),
    )
    parent_id: str | None = Field(
        default=None,
        description="Parent agent ID (null for root)",
    )
    children: list[str] = Field(
        default_factory=list,
        description="Child agent IDs",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Available tools for this agent",
    )
    can_delegate: bool = Field(
        default=False,
        description="Whether this agent can delegate to children",
    )
    shared_kb_permissions: list[SharedKBPermission] = Field(
        default_factory=list,
        description="Shared knowledge base access rules",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional role-specific configuration",
    )

    @model_validator(mode="after")
    def _validate_pool(self) -> AgentConfig:
        if self.pool is not None and self.kind != "service":
            raise ValueError(
                f"Agent '{self.agent_id}': pool config requires "
                f"kind='service' (got '{self.kind}')",
            )
        return self
