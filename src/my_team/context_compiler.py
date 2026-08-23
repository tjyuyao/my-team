"""ContextCompiler — role-aware observation assembly with token budget.

Per SPEC §5: agents see context appropriate to their role. Root sees
global KPI and task tree summary; workers see focus task details and
relevant KB entries. Token budget constrains total observation size.

Usage:
    compiler = ContextCompiler(agent_tree, task_tree, shared_kb, mail_system)
    observation = compiler.compile(agent_config, snapshot, continuation)
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TaskScope(str, Enum):
    """Controls which tasks an agent can see."""

    FOCUS = "focus"      # Only the current task (from continuation)
    OWNED = "owned"      # All tasks owned by this agent
    SUBTREE = "subtree"  # This agent's tasks + children's tasks
    ALL = "all"          # Full task tree


class ObservationSection(str, Enum):
    """Sections that can be included in an observation."""

    MISSION = "mission"
    TASK_TREE_SUMMARY = "task_tree_summary"
    TASK_DETAIL = "task_detail"
    KPI_DASHBOARD = "kpi_dashboard"
    EMAILS = "emails"
    KB_SNAPSHOT = "kb_snapshot"
    WORKSPACE_FILES = "workspace_files"
    LOCK_STATES = "lock_states"
    ESCALATIONS = "escalations"
    PENDING_DECISIONS = "pending_decisions"


class ObservationPolicy(BaseModel):
    """Defines what an agent sees and how much."""

    sections: list[ObservationSection] = Field(
        default_factory=lambda: [
            ObservationSection.TASK_DETAIL,
            ObservationSection.EMAILS,
        ],
        description="Which sections to include, in order",
    )
    task_scope: TaskScope = Field(
        default=TaskScope.ALL,
        description="Which tasks are visible",
    )
    kb_injection: bool = Field(
        default=True,
        description="Whether to inject KB paths/versions",
    )
    max_tokens: int = Field(
        default=8000,
        description="Approximate token budget for the observation",
    )
    include_email_body: bool = Field(
        default=True,
        description="Whether to include full email body",
    )


# -- Default policies per role -----------------------------------------------

DEFAULT_POLICIES: dict[str, ObservationPolicy] = {
    "root_decision_agent": ObservationPolicy(
        sections=[
            ObservationSection.MISSION,
            ObservationSection.TASK_TREE_SUMMARY,
            ObservationSection.KPI_DASHBOARD,
            ObservationSection.ESCALATIONS,
            ObservationSection.PENDING_DECISIONS,
            ObservationSection.EMAILS,
            ObservationSection.KB_SNAPSHOT,
        ],
        task_scope=TaskScope.ALL,
        max_tokens=8000,
    ),
    "manager": ObservationPolicy(
        sections=[
            ObservationSection.TASK_TREE_SUMMARY,
            ObservationSection.TASK_DETAIL,
            ObservationSection.EMAILS,
            ObservationSection.KB_SNAPSHOT,
        ],
        task_scope=TaskScope.SUBTREE,
        max_tokens=6000,
    ),
    "worker": ObservationPolicy(
        sections=[
            ObservationSection.TASK_DETAIL,
            ObservationSection.EMAILS,
            ObservationSection.WORKSPACE_FILES,
            ObservationSection.KB_SNAPSHOT,
        ],
        task_scope=TaskScope.FOCUS,
        max_tokens=4000,
    ),
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


class ContextCompiler:
    """Assembles role-aware observations from the frozen snapshot.

    Sits between the raw snapshot and the AgentObservation, applying:
    - Role-based section selection
    - Task scope filtering
    - Email recipient filtering
    - Token budget enforcement (email body truncation)
    """

    def __init__(
        self,
        agent_tree: Any,
        task_tree: Any,
        shared_kb: Any,
        mail_system: Any,
        private_store: Any,
        policies: dict[str, ObservationPolicy] | None = None,
    ) -> None:
        self._agent_tree = agent_tree
        self._task_tree = task_tree
        self._shared_kb = shared_kb
        self._mail_system = mail_system
        self._private_store = private_store
        self._policies = dict(policies or DEFAULT_POLICIES)

    def get_policy(self, role: str) -> ObservationPolicy:
        """Get the observation policy for a role."""
        return self._policies.get(role, ObservationPolicy())

    def compile(
        self,
        agent_config: Any,
        snapshot: dict[str, Any],
        continuation: Any | None = None,
    ) -> dict[str, Any]:
        """Compile a role-aware observation from the snapshot.

        Returns a dict compatible with AgentObservation fields.
        """
        agent_id = agent_config.agent_id
        role = agent_config.role
        policy = self.get_policy(role)

        result: dict[str, Any] = {
            "agent_id": agent_id,
            "tick": snapshot.get("tick", 0),
            "emails": [],
            "task_states": {},
            "shared_kb_snapshot": {},
            "lock_states": {},
            "private_workspace_path": str(
                self._private_store.agent_home(agent_id),
            ),
        }

        tokens_used = 0
        max_tokens = policy.max_tokens

        for section in policy.sections:
            if tokens_used >= max_tokens:
                break

            if section == ObservationSection.MISSION:
                tokens_used += self._add_mission(result, agent_config)

            elif section == ObservationSection.TASK_TREE_SUMMARY:
                tokens_used += self._add_task_summary(result, snapshot)

            elif section == ObservationSection.TASK_DETAIL:
                tokens_used += self._add_task_detail(
                    result, snapshot, policy, continuation,
                )

            elif section == ObservationSection.KPI_DASHBOARD:
                tokens_used += self._add_kpi(result, snapshot)

            elif section == ObservationSection.EMAILS:
                tokens_used += self._add_emails(
                    result, snapshot, agent_id, policy, max_tokens - tokens_used,
                )

            elif section == ObservationSection.KB_SNAPSHOT:
                if policy.kb_injection:
                    tokens_used += self._add_kb(result, snapshot)

            elif section == ObservationSection.WORKSPACE_FILES:
                tokens_used += self._add_workspace(result, snapshot, agent_id)

            elif section == ObservationSection.LOCK_STATES:
                tokens_used += self._add_locks(result, snapshot, agent_id)

            elif section == ObservationSection.ESCALATIONS:
                tokens_used += self._add_escalations(result, snapshot)

            elif section == ObservationSection.PENDING_DECISIONS:
                tokens_used += self._add_pending_decisions(result, snapshot)

        return result

    def _add_mission(self, result: dict, config: Any) -> int:
        """Add mission statement from agent metadata."""
        mission = config.metadata.get("mission", "")
        if mission:
            result["mission"] = mission
            return _estimate_tokens(mission)
        return 0

    def _add_task_summary(self, result: dict, snapshot: dict) -> int:
        """Add a summary of all tasks (count by status)."""
        tasks = snapshot.get("tasks", {})
        summary: dict[str, int] = {}
        for task_data in tasks.values():
            status = task_data.get("status", "unknown")
            summary[status] = summary.get(status, 0) + 1
        result["task_summary"] = summary
        result["task_states"] = dict(tasks)  # include full task data for root
        return _estimate_tokens(str(summary))

    def _add_task_detail(
        self,
        result: dict,
        snapshot: dict,
        policy: ObservationPolicy,
        continuation: Any | None,
    ) -> int:
        """Add tasks filtered by scope."""
        tasks = snapshot.get("tasks", {})
        agent_id = result["agent_id"]
        filtered: dict[str, dict] = {}

        if policy.task_scope == TaskScope.ALL:
            filtered = dict(tasks)
        elif policy.task_scope == TaskScope.OWNED:
            filtered = {
                k: v for k, v in tasks.items()
                if v.get("assignee") == agent_id
            }
        elif policy.task_scope == TaskScope.FOCUS:
            focus_task_id = ""
            if continuation is not None:
                focus_task_id = getattr(continuation, "task_id", "")
            if focus_task_id and focus_task_id in tasks:
                filtered = {focus_task_id: tasks[focus_task_id]}
        elif policy.task_scope == TaskScope.SUBTREE:
            # Include owned tasks + children's tasks
            agent_config = None
            for cfg in self._agent_tree:
                if cfg.agent_id == agent_id:
                    agent_config = cfg
                    break
            child_ids = set(agent_config.children) if agent_config else set()
            filtered = {
                k: v for k, v in tasks.items()
                if v.get("assignee") == agent_id or v.get("assignee") in child_ids
            }

        result["task_states"] = filtered
        return _estimate_tokens(str(filtered))

    def _add_kpi(self, result: dict, snapshot: dict) -> int:
        """Add KPI dashboard (task completion stats)."""
        tasks = snapshot.get("tasks", {})
        total = len(tasks)
        completed = sum(
            1 for t in tasks.values()
            if t.get("status") in ("completed", "failed")
        )
        in_progress = sum(
            1 for t in tasks.values()
            if t.get("status") == "in_progress"
        )
        kpi = {
            "total_tasks": total,
            "completed": completed,
            "in_progress": in_progress,
            "completion_rate": f"{completed/total*100:.0f}%" if total else "0%",
        }
        result["kpi"] = kpi
        return _estimate_tokens(str(kpi))

    def _add_emails(
        self,
        result: dict,
        snapshot: dict,
        agent_id: str,
        policy: ObservationPolicy,
        remaining_budget: int,
    ) -> int:
        """Add emails addressed to this agent, with body truncation."""
        all_emails = snapshot.get("emails", [])
        agent_emails = [
            e for e in all_emails
            if agent_id in e.get("to", [])
        ]

        if not policy.include_email_body:
            # Strip body to save tokens
            agent_emails = [
                {k: v for k, v in e.items() if k != "body"}
                for e in agent_emails
            ]
            result["emails"] = agent_emails
            return _estimate_tokens(str(agent_emails))

        # Include bodies, but truncate if over budget
        tokens_used = 0
        processed = []
        for email in agent_emails:
            email_str = str(email)
            email_tokens = _estimate_tokens(email_str)
            if tokens_used + email_tokens > remaining_budget:
                # Truncate body
                truncated = dict(email)
                body = truncated.get("body", "")
                max_body_tokens = remaining_budget - tokens_used - 20
                if max_body_tokens > 0:
                    truncated["body"] = body[:max_body_tokens * 4] + "... [truncated]"
                else:
                    truncated["body"] = "... [truncated]"
                processed.append(truncated)
                tokens_used += _estimate_tokens(str(truncated))
                break
            processed.append(email)
            tokens_used += email_tokens

        result["emails"] = processed
        return tokens_used

    def _add_kb(self, result: dict, snapshot: dict) -> int:
        """Add shared KB paths and versions."""
        kb = snapshot.get("shared_kb", {})
        result["shared_kb_snapshot"] = dict(kb)
        return _estimate_tokens(str(kb))

    def _add_workspace(self, result: dict, snapshot: dict, agent_id: str) -> int:
        """Add workspace file listing."""
        private_files = snapshot.get("private_files", {})
        agent_files = private_files.get(agent_id, {})
        file_list = list(agent_files.get("files", {}).keys())
        result["workspace_files"] = file_list
        return _estimate_tokens(str(file_list))

    def _add_locks(self, result: dict, snapshot: dict, agent_id: str) -> int:
        """Add lock states (filtered per agent)."""
        locks = snapshot.get("locks", {})
        lock_tokens = snapshot.get("lock_tokens", {})
        agent_locks = {}
        for resource, lock_info in locks.items():
            entry = dict(lock_info)
            if entry.get("owner") == agent_id and resource in lock_tokens:
                entry["lock_token"] = lock_tokens[resource]
            agent_locks[resource] = entry
        result["lock_states"] = agent_locks
        return _estimate_tokens(str(agent_locks))

    def _add_escalations(self, result: dict, snapshot: dict) -> int:
        """Add escalated tasks (overdue or blocked)."""
        tasks = snapshot.get("tasks", {})
        escalations = []
        for task_id, task_data in tasks.items():
            status = task_data.get("status", "")
            if status in ("failed", "blocked"):
                escalations.append({
                    "task_id": task_id,
                    "status": status,
                    "title": task_data.get("title", ""),
                })
        result["escalations"] = escalations
        return _estimate_tokens(str(escalations))

    def _add_pending_decisions(self, result: dict, snapshot: dict) -> int:
        """Add tasks awaiting decision (draft or assigned)."""
        tasks = snapshot.get("tasks", {})
        pending = []
        for task_id, task_data in tasks.items():
            status = task_data.get("status", "")
            if status in ("draft", "assigned"):
                pending.append({
                    "task_id": task_id,
                    "status": status,
                    "title": task_data.get("title", ""),
                    "assignee": task_data.get("assignee", ""),
                })
        result["pending_decisions"] = pending
        return _estimate_tokens(str(pending))
