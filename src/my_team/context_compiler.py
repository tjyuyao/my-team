"""ContextCompiler — 三预算注入管线（N4-3 重写）。

对应 N4_MEMORY_INJECTION_DESIGN.md §3/§4 与 SPEC §4.2/§4.3。

**设计立场（N4-3）：**
- 保留 ``ContextCompiler`` 类名与 ``compile()`` 签名——存量测试与
  _phase_observe 接线零冲击；
- ``DEFAULT_POLICIES``/``ObservationPolicy``/``ObservationSection``/
  ``TaskScope`` 保留导出（存量测试 import 不报错），但 ``compile()``
  **不再读取 role 对应策略**——role 参数只保留签名停止消费语义；
- 三预算注入布局：
  ① 固定注入（priority<10，fixed_memory_tokens 单独预算不可超可配置，
     超限置 pending_consolidation 标志）；
  ② 召回注入（priority≥10 命中 + 召回命中，top-k + 详细度降级
     full/summary/title-only）；
  ③ 观察上下文（emails/tasks/KB/locks 等原 section 内容，剩余预算）；
- 来源段不可覆盖：POLICY/[POSITION_JD] 等高优先级注入最先且不可被
  覆盖（source_tag 驱动，有测试）；客户内容永不作系统指令；
- 版本戳入 Journal：compile() 调用后自动写 MEMORY_INJECTION_STAMP
  audit 事件（布局引用 + 顺序 + 详细度 + 版本戳，非内容快照）；
  AgentObservation.memory_injection 字段携带布局元数据（兼容旧代码）。

原有的 ``ObservationPolicy``/``ObservationSection``/``TaskScope``/
``DEFAULT_POLICIES`` 类保留导出兼容，不从外部导入删掉。
"""

from __future__ import annotations

import hashlib
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from my_team.audit import AuditLog
    from my_team.devices.authority import Authority
    from my_team.memory_recall import RecallConfig, RecallEngine
    from my_team.memory_store import AgentMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 来源段枚举（source_tag 对应 N4_MEMORY_INJECTION_DESIGN §4）
# ---------------------------------------------------------------------------

# 来源段不可覆盖的高优先级标签集合
# POLICY/POSITION_JD 最先注入，不可被后续内容覆盖
_UNOVERRIDABLE_SOURCE_TAGS: frozenset[str] = frozenset({
    "[POLICY]",
    "[POSITION_JD]",
})

# 客户内容标签——永不作系统指令（§8.4，安全不变量）
_UNTRUSTED_SOURCE_TAGS: frozenset[str] = frozenset({
    "[UNTRUSTED_CUSTOMER_CONTENT]",
})

# 详细度枚举
class DetailLevel(str, Enum):
    """注入条目详细度（预算紧时降级）。"""
    FULL = "full"         # 完整 content
    SUMMARY = "summary"   # 仅 title + memory_points 摘要
    TITLE_ONLY = "title_only"  # 仅 title


# ---------------------------------------------------------------------------
# 注入条目（注入布局中的一个单元）
# ---------------------------------------------------------------------------

class InjectionSlot:
    """注入布局中的一个条目槽（来源段 + 优先级 + 详细度 + 内容）。

    供内部管线使用，不对外暴露。
    """

    __slots__ = (
        "source_tag", "priority_class", "detail_level",
        "content", "entry_ref", "stamp_token",
    )

    def __init__(
        self,
        source_tag: str,
        priority_class: str,   # "fixed" | "recalled" | "context"
        detail_level: DetailLevel,
        content: str,
        entry_ref: str = "",   # entry_id/entity_id（用于布局版本戳）
        stamp_token: str = "",  # version_tuple/content_hash（版本戳）
    ) -> None:
        self.source_tag = source_tag
        self.priority_class = priority_class
        self.detail_level = detail_level
        self.content = content
        self.entry_ref = entry_ref
        self.stamp_token = stamp_token

    def is_untrusted(self) -> bool:
        """是否为客户不可信内容（永不作系统指令）。"""
        return self.source_tag in _UNTRUSTED_SOURCE_TAGS

    def is_unoverridable(self) -> bool:
        """是否为不可覆盖段（POLICY/POSITION_JD）。"""
        return self.source_tag in _UNOVERRIDABLE_SOURCE_TAGS


# ---------------------------------------------------------------------------
# 注入布局结果
# ---------------------------------------------------------------------------

class InjectionLayout:
    """三预算注入布局结果（传递给 render_system_prompt）。

    Attributes:
        fixed_slots: 固定注入槽（priority<10，已过滤不可信）；
        recalled_slots: 召回注入槽（priority≥10，按 trigger_score 降序）；
        context_sections: 观察上下文内容片段（emails/tasks/KB 等原文）；
        pending_consolidation: 固定预算超限标志（只读，Observe 无副作用）；
        layout_refs: 条目引用列表（entry_ref 列表，用于版本戳）；
        stamp_hash: 布局内容哈希（版本戳，非内容快照）。
    """

    def __init__(
        self,
        fixed_slots: list[InjectionSlot],
        recalled_slots: list[InjectionSlot],
        context_sections: dict[str, Any],
        pending_consolidation: bool = False,
    ) -> None:
        self.fixed_slots = fixed_slots
        self.recalled_slots = recalled_slots
        self.context_sections = context_sections
        self.pending_consolidation = pending_consolidation

        # 计算布局版本戳
        self.layout_refs: list[str] = [
            s.entry_ref for s in (fixed_slots + recalled_slots) if s.entry_ref
        ]
        stamp_src = "|".join(
            f"{s.entry_ref}:{s.stamp_token}:{s.detail_level.value}"
            for s in (fixed_slots + recalled_slots)
        )
        self.stamp_hash = hashlib.sha256(stamp_src.encode()).hexdigest()[:16]

    def to_meta_dict(self) -> dict[str, Any]:
        """导出为 AgentObservation.memory_injection 元数据字典。"""
        return {
            "layout_refs": self.layout_refs,
            "detail_levels": {
                s.entry_ref: s.detail_level.value
                for s in (self.fixed_slots + self.recalled_slots)
                if s.entry_ref
            },
            "stamp_hash": self.stamp_hash,
            "pending_consolidation": self.pending_consolidation,
            "fixed_count": len(self.fixed_slots),
            "recalled_count": len(self.recalled_slots),
        }


# ---------------------------------------------------------------------------
# 存量 API 兼容层（原 ObservationPolicy/TaskScope/ObservationSection）
# 仅保留导出以免 import 报错，compile() 内部不读取
# ---------------------------------------------------------------------------

class TaskScope(str, Enum):
    """Controls which tasks an agent can see（兼容桥，N4-3 内部不读取）。"""
    FOCUS = "focus"
    OWNED = "owned"
    SUBTREE = "subtree"
    ALL = "all"


class ObservationSection(str, Enum):
    """Sections that can be included in an observation（兼容桥）。"""
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
    """Defines what an agent sees and how much（兼容桥，N4-3 内部不读取）。"""

    sections: list[ObservationSection] = Field(
        default_factory=lambda: [
            ObservationSection.TASK_DETAIL,
            ObservationSection.EMAILS,
        ],
    )
    task_scope: TaskScope = Field(default=TaskScope.ALL)
    kb_injection: bool = Field(default=True)
    max_tokens: int = Field(default=8000)
    include_email_body: bool = Field(default=True)


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
    """粗略 token 估算：约 4 字符/token。"""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# 三预算注入组装器（ContextCompiler 重写）
# ---------------------------------------------------------------------------

class ContextCompiler:
    """三预算注入管线（N4-3 重写，保留原类名与 compile() 签名）。

    三预算布局：
    ① 固定注入（priority<10）——来自 Authority.injection_for()，按 priority
       升序（最小 = 最高优）排列，POLICY/[POSITION_JD] 最先且不可覆盖；
       fixed_memory_tokens 单独预算，超限置 pending_consolidation；
    ② 召回注入（priority≥10 命中 + RecallEngine 召回，按 trigger_score
       降序），详细度可降级（full→summary→title-only）；
    ③ 观察上下文（emails/tasks/KB/locks 等原 section 内容）：剩余预算。

    同一 entity 多 position 授予去重取最高 priority（最小数值）。
    来源段不可覆盖：POLICY/[POSITION_JD] 不可被 skill/客户内容覆盖。
    版本戳入 Journal：compile() 完成后写 MEMORY_INJECTION_STAMP audit 事件。

    存量 API：``get_policy(role)`` 仍可用（返回 DEFAULT_POLICIES 兼容值），
    但 ``compile()`` 内部不再读取 role 语义，role 参数保留签名停止消费。
    """

    def __init__(
        self,
        agent_tree: Any,
        task_tree: Any,
        shared_kb: Any,
        mail_system: Any,
        private_store: Any,
        policies: dict[str, ObservationPolicy] | None = None,
        # N4-3 新增（可选）：Authority + 记忆子系统
        authority: "Authority | None" = None,
        agent_memories: "dict[str, AgentMemory] | None" = None,
        recall_configs: "dict[str, RecallConfig] | None" = None,
        recall_engines: "dict[str, RecallEngine] | None" = None,
        audit_log: "AuditLog | None" = None,
        # 预算配置（可由 ConfigDevice.memory_budget 传入）
        fixed_memory_tokens: int = 4_000,
        recall_memory_tokens: int = 8_000,
        context_tokens: int = 8_000,
        recall_top_k: int = 10,
    ) -> None:
        self._agent_tree = agent_tree
        self._task_tree = task_tree
        self._shared_kb = shared_kb
        self._mail_system = mail_system
        self._private_store = private_store
        self._policies = dict(policies or DEFAULT_POLICIES)
        # N4-3 注入子系统（可选，无时降级到观察上下文模式）
        self._authority = authority
        self._agent_memories: dict[str, AgentMemory] = dict(agent_memories or {})
        self._recall_configs: dict[str, RecallConfig] = dict(recall_configs or {})
        self._recall_engines: dict[str, RecallEngine] = dict(recall_engines or {})
        self._audit_log = audit_log
        # 预算参数
        self._fixed_memory_tokens = fixed_memory_tokens
        self._recall_memory_tokens = recall_memory_tokens
        self._context_tokens = context_tokens
        self._recall_top_k = recall_top_k

    # ------------------------------------------------------------------
    # 存量 API 兼容
    # ------------------------------------------------------------------

    def get_policy(self, role: str) -> ObservationPolicy:
        """Get the observation policy for a role（兼容存量测试）。"""
        return self._policies.get(role, ObservationPolicy())

    # ------------------------------------------------------------------
    # 主编译接口
    # ------------------------------------------------------------------

    def compile(
        self,
        agent_config: Any,
        snapshot: dict[str, Any],
        continuation: Any | None = None,
    ) -> dict[str, Any]:
        """三预算注入管线——编译 AgentObservation 兼容字典。

        role 参数（agent_config.role）保留签名，N4-3 停止语义消费。

        返回 AgentObservation 兼容字典（含 memory_injection 布局元数据）。
        """
        agent_id = agent_config.agent_id

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
            "memory_injection": {},
        }

        # ① 固定注入（priority<10）
        fixed_slots, pending_consolidation = self._build_fixed_injection(agent_id)

        # ② 召回注入（priority≥10 + 召回引擎）
        # 召回上下文词：从快照中提取 task/email 关键词
        contextual_terms = self._extract_contextual_terms(agent_id, snapshot, continuation)
        recalled_slots = self._build_recalled_injection(agent_id, contextual_terms)

        # ③ 观察上下文（emails/tasks/KB/locks 等原 section 内容）
        policy = self.get_policy(getattr(agent_config, "role", "worker"))
        self._fill_context_sections(
            result, agent_id, snapshot, continuation, policy,
        )

        # 组装布局
        layout = InjectionLayout(
            fixed_slots=fixed_slots,
            recalled_slots=recalled_slots,
            context_sections=result,
            pending_consolidation=pending_consolidation,
        )
        result["memory_injection"] = layout.to_meta_dict()

        # 版本戳入 Journal（audit log）
        if self._audit_log is not None:
            try:
                from my_team.audit import AuditEventType
                self._audit_log.record(
                    event_type=AuditEventType.MEMORY_INJECTION_STAMP,
                    agent_id=agent_id,
                    tick=snapshot.get("tick", 0),
                    details=layout.to_meta_dict(),
                )
            except Exception:  # noqa: BLE001
                logger.warning("MEMORY_INJECTION_STAMP 记录失败", exc_info=True)

        return result

    # ------------------------------------------------------------------
    # ① 固定注入管线（priority<10）
    # ------------------------------------------------------------------

    def _build_fixed_injection(
        self,
        agent_id: str,
    ) -> tuple[list[InjectionSlot], bool]:
        """组装固定注入槽（priority<10）。

        - 来自 Authority.injection_for()（同一 entity 多 position 去重取最小 priority）；
        - POLICY/[POSITION_JD] 最先且不可覆盖；
        - fixed_memory_tokens 单独预算不可超，超限置 pending_consolidation；
        - 客户不可信内容不进固定注入（来源段安全不变量）。

        Returns:
            (fixed_slots 列表, pending_consolidation 标志)
        """
        if self._authority is None:
            return [], False

        # 从 authority 获取全部注入
        raw_injections = self._authority.injection_for(agent_id)

        # 去重：同一 entity_id 多 position 授予 → 取最小 priority（最高优先）
        best: dict[str, Any] = {}  # entity_id → MemoryInjection
        for inj in raw_injections:
            eid = inj.entity_id
            if eid not in best or inj.priority < best[eid].priority:
                best[eid] = inj

        # 过滤：只取 priority<10
        fixed_injections = [inj for inj in best.values() if inj.priority < 10]

        # 排序：priority 升序（POLICY[priority=0]/POSITION_JD[priority=1] 最先）
        fixed_injections.sort(key=lambda inj: (inj.priority, inj.entity_id))

        slots: list[InjectionSlot] = []
        tokens_used = 0
        pending_consolidation = False

        for inj in fixed_injections:
            tag = inj.source_tag or "[MEMORY]"

            # 客户不可信内容永不进固定注入（安全不变量 §8.4）
            if tag in _UNTRUSTED_SOURCE_TAGS:
                logger.debug(
                    "固定注入跳过不可信内容：entity_id=%s source_tag=%s",
                    inj.entity_id, tag,
                )
                continue

            content = inj.content
            tok = _estimate_tokens(content)

            if tokens_used + tok > self._fixed_memory_tokens:
                # 固定预算超限：置 pending_consolidation 标志（Observe 只读，无副作用）
                pending_consolidation = True
                logger.debug(
                    "固定注入预算超限：agent=%s entity=%s tok=%d used=%d budget=%d",
                    agent_id, inj.entity_id, tok, tokens_used, self._fixed_memory_tokens,
                )
                continue

            stamp_token = f"v:{inj.priority}:{hashlib.sha256(content.encode()).hexdigest()[:8]}"
            slots.append(InjectionSlot(
                source_tag=tag,
                priority_class="fixed",
                detail_level=DetailLevel.FULL,
                content=content,
                entry_ref=inj.entity_id,
                stamp_token=stamp_token,
            ))
            tokens_used += tok

        return slots, pending_consolidation

    # ------------------------------------------------------------------
    # ② 召回注入管线（priority≥10 命中 + RecallEngine）
    # ------------------------------------------------------------------

    def _build_recalled_injection(
        self,
        agent_id: str,
        contextual_terms: list[str],
    ) -> list[InjectionSlot]:
        """组装召回注入槽（priority≥10 + 召回引擎 top-k + 详细度降级）。

        - 先从 authority 取 priority≥10 的外加载注入（不入 store，投影只读）；
        - 再从 RecallEngine 取 top-k 条目（来自 AgentMemory）；
        - 去重合并（entity_id 相同取一次）；
        - 详细度降级（full→summary→title-only）在 recall_memory_tokens 预算内。
        """
        slots: list[InjectionSlot] = []

        # a) priority≥10 的外加载注入（来自 Authority）
        if self._authority is not None:
            raw = self._authority.injection_for(agent_id)
            best: dict[str, Any] = {}
            for inj in raw:
                eid = inj.entity_id
                if eid not in best or inj.priority < best[eid].priority:
                    best[eid] = inj

            for inj in sorted(best.values(), key=lambda x: x.priority):
                if inj.priority < 10:
                    continue
                tag = inj.source_tag or "[MEMORY]"
                if tag in _UNTRUSTED_SOURCE_TAGS:
                    continue
                content = inj.content
                stamp = f"v:{inj.priority}:{hashlib.sha256(content.encode()).hexdigest()[:8]}"
                slots.append(InjectionSlot(
                    source_tag=tag,
                    priority_class="recalled",
                    detail_level=DetailLevel.FULL,
                    content=content,
                    entry_ref=inj.entity_id,
                    stamp_token=stamp,
                ))

        # b) RecallEngine 召回（AgentMemory 自有条目）
        recall_engine = self._recall_engines.get(agent_id)
        recall_config = self._recall_configs.get(agent_id)
        agent_memory = self._agent_memories.get(agent_id)

        if recall_engine is not None and recall_config is not None and agent_memory is not None:
            try:
                candidates = recall_engine.recall(
                    store=agent_memory,
                    config=recall_config,
                    contextual_terms=contextual_terms,
                    top_k=self._recall_top_k,
                    consume_temp_overrides=True,
                )
                for cand in candidates:
                    eid = str(cand.entry.entry_id)
                    # 去重（authority 外加载已有的 entity_id 不再重复）
                    existing_refs = {s.entry_ref for s in slots}
                    if eid in existing_refs:
                        continue
                    entry = cand.entry
                    stamp = f"v{entry.version}:{str(entry.entry_id)[:8]}"
                    slots.append(InjectionSlot(
                        source_tag=cand.source_tag or "[MEMORY]",
                        priority_class="recalled",
                        detail_level=DetailLevel.FULL,
                        content=self._render_entry_full(entry),
                        entry_ref=eid,
                        stamp_token=stamp,
                    ))
            except Exception:  # noqa: BLE001
                logger.warning("RecallEngine 召回失败", exc_info=True)

        # 在 recall_memory_tokens 预算内做详细度降级
        return self._apply_detail_budget(slots, self._recall_memory_tokens)

    def _apply_detail_budget(
        self,
        slots: list[InjectionSlot],
        budget: int,
    ) -> list[InjectionSlot]:
        """在 recall 预算内对注入槽做详细度降级（full→summary→title-only）。

        逐槽尝试 FULL → SUMMARY → TITLE_ONLY，直到预算内可容纳为止。
        """
        final: list[InjectionSlot] = []
        used = 0

        for slot in slots:
            content_full = slot.content
            summary = f"[{slot.source_tag}] {self._get_title(slot)}: {self._get_summary(slot)}"
            title_only = f"[{slot.source_tag}] {self._get_title(slot)}"

            for level, content in [
                (DetailLevel.FULL, content_full),
                (DetailLevel.SUMMARY, summary),
                (DetailLevel.TITLE_ONLY, title_only),
            ]:
                tok = _estimate_tokens(content)
                if used + tok <= budget:
                    final.append(InjectionSlot(
                        source_tag=slot.source_tag,
                        priority_class=slot.priority_class,
                        detail_level=level,
                        content=content,
                        entry_ref=slot.entry_ref,
                        stamp_token=slot.stamp_token,
                    ))
                    used += tok
                    break
            # 如果连 TITLE_ONLY 都超预算则跳过

        return final

    # ------------------------------------------------------------------
    # ③ 观察上下文（原 section 内容，剩余预算）
    # ------------------------------------------------------------------

    def _fill_context_sections(
        self,
        result: dict[str, Any],
        agent_id: str,
        snapshot: dict[str, Any],
        continuation: Any | None,
        policy: ObservationPolicy | None = None,
    ) -> None:
        """填充观察上下文段（emails/tasks/KB/locks 等），剩余预算内。

        policy 控制段开关与预算（兼容存量测试语义）：
        - kb_injection=False → shared_kb_snapshot 为空；
        - max_tokens → emails body 超限截断（"... [truncated]"）。
        policy 为 None 时全量填充（无预算约束）。
        """
        # Mission（来自 metadata）
        mission = getattr(
            self._get_agent_config(agent_id), "metadata", {}
        ).get("mission", "")
        if mission:
            result["mission"] = mission

        # Task states：focus_task_id 优先，否则全量
        tasks = snapshot.get("tasks", {})
        focus_task_id = getattr(continuation, "task_id", "") if continuation else ""
        if focus_task_id and focus_task_id in tasks:
            result["task_states"] = {focus_task_id: dict(tasks[focus_task_id])}
        else:
            result["task_states"] = dict(tasks)

        # Task summary（全量统计）
        summary: dict[str, int] = {}
        for td in tasks.values():
            s = td.get("status", "unknown")
            summary[s] = summary.get(s, 0) + 1
        result["task_summary"] = summary

        # KPI
        total = len(tasks)
        completed = sum(1 for t in tasks.values() if t.get("status") in ("completed", "failed"))
        in_progress = sum(1 for t in tasks.values() if t.get("status") == "in_progress")
        result["kpi"] = {
            "total_tasks": total,
            "completed": completed,
            "in_progress": in_progress,
            "completion_rate": f"{completed/total*100:.0f}%" if total else "0%",
        }

        # Emails（过滤收件人 + 预算截断）
        all_emails = snapshot.get("emails", [])
        agent_emails = [e for e in all_emails if agent_id in e.get("to", [])]
        max_tokens = policy.max_tokens if policy is not None else 0
        result["emails"] = [
            self._budget_truncate_email(e, max_tokens) for e in agent_emails
        ]

        # KB 快照（kb_injection 开关）
        if policy is not None and not policy.kb_injection:
            result["shared_kb_snapshot"] = {}
        else:
            kb = snapshot.get("shared_kb", {})
            result["shared_kb_snapshot"] = dict(kb)

        # Workspace files
        private_files = snapshot.get("private_files", {})
        agent_files = private_files.get(agent_id, {})
        result["workspace_files"] = list(agent_files.get("files", {}).keys())

        # Lock states
        locks = snapshot.get("locks", {})
        lock_tokens = snapshot.get("lock_tokens", {})
        agent_locks = {}
        for resource, lock_info in locks.items():
            entry = dict(lock_info)
            if entry.get("owner") == agent_id and resource in lock_tokens:
                entry["lock_token"] = lock_tokens[resource]
            agent_locks[resource] = entry
        result["lock_states"] = agent_locks

        # Escalations
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

        # Pending decisions
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

    @staticmethod
    def _budget_truncate_email(
        email: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        """按预算截断邮件 body（旧语义：超限加 '... [truncated]'）。

        max_tokens<=0 视为无预算约束（全量）。
        """
        if max_tokens <= 0:
            return email
        body = email.get("body", "")
        if _estimate_tokens(body) <= max_tokens:
            return email
        truncated = dict(email)
        max_body_tokens = max(max_tokens // 2, 1)
        if body:
            truncated["body"] = body[:max_body_tokens * 4] + "... [truncated]"
        else:
            truncated["body"] = "... [truncated]"
        return truncated

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get_agent_config(self, agent_id: str) -> Any:
        """从 agent_tree 找到 agent config（找不到返回 None）。"""
        for cfg in self._agent_tree:
            if cfg.agent_id == agent_id:
                return cfg
        return None

    def _extract_contextual_terms(
        self,
        agent_id: str,
        snapshot: dict[str, Any],
        continuation: Any | None,
    ) -> list[str]:
        """从快照中提取本 tick 上下文词（供召回引擎使用）。

        来源：focus task 的 title/status、inbox 邮件的 subject。
        """
        terms: list[str] = []

        tasks = snapshot.get("tasks", {})
        focus_task_id = getattr(continuation, "task_id", "") if continuation else ""
        if focus_task_id and focus_task_id in tasks:
            td = tasks[focus_task_id]
            if td.get("title"):
                terms.append(td["title"])
            if td.get("status"):
                terms.append(td["status"])

        for email in snapshot.get("emails", []):
            if agent_id in email.get("to", []):
                subj = email.get("subject", "")
                if subj:
                    terms.append(subj)

        return terms

    @staticmethod
    def _render_entry_full(entry: Any) -> str:
        """渲染 MemoryEntry 为 full 详细度文本。"""
        from my_team.models.memory import MemoryEntry
        if not isinstance(entry, MemoryEntry):
            return str(entry)
        lines = [f"[{entry.type.value.upper()}] {entry.title}"]
        content = entry.content
        for field_name, val in content.model_dump().items():
            if val:
                lines.append(f"  {field_name}: {val}")
        if entry.memory_points:
            lines.append(f"  triggers: {', '.join(entry.memory_points)}")
        return "\n".join(lines)

    @staticmethod
    def _get_title(slot: InjectionSlot) -> str:
        """从槽内容提取标题（取首行）。"""
        return slot.content.split("\n")[0].strip()

    @staticmethod
    def _get_summary(slot: InjectionSlot) -> str:
        """从槽内容提取摘要（取前2行，去空白）。"""
        lines = [line.strip() for line in slot.content.split("\n")[:3] if line.strip()]
        return " | ".join(lines[:2]) if lines else ""

    # ------------------------------------------------------------------
    # InjectionLayout 公开访问（测试用）
    # ------------------------------------------------------------------

    def build_layout(
        self,
        agent_id: str,
        snapshot: dict[str, Any],
        continuation: Any | None = None,
    ) -> InjectionLayout:
        """直接组装注入布局（测试/审计场景，不写 Journal）。"""
        fixed_slots, pending_consolidation = self._build_fixed_injection(agent_id)
        contextual_terms = self._extract_contextual_terms(agent_id, snapshot, continuation)
        recalled_slots = self._build_recalled_injection(agent_id, contextual_terms)
        return InjectionLayout(
            fixed_slots=fixed_slots,
            recalled_slots=recalled_slots,
            context_sections={},
            pending_consolidation=pending_consolidation,
        )
