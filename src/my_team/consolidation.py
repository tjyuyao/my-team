"""整理模式 CONSOLIDATING（N4-4）。

对应 SPEC §4.4 与 N4_MEMORY_INJECTION_DESIGN.md §5（v2 扩展）。

**不只压缩，更是「反思与进步」**——取代 harness 的固定总结提示词：

- 触发：① 预算触发（组装器 pending_consolidation 标志 / 固定注入
  使用率，Observe 只读）；② agent 主动发起（MemoryConsolidateIntent，
  不限于预算满）；hysteresis 进 90% / 出 80% 防连续 tick 抖动；
- 工具面收窄：CONSOLIDATING 下授权集 = 记忆工具集
  （memory_fold/promote/edit/retag/evict/pin）；
- 输出 = 整理动作序列 + **结构化摘要**（反思与进步、经验教训、
  流程优化、记忆之间建立链接）；摘要本身作为 MemoryEntry 写入
  （type=skill，provenance 记整理来源）；
- JUDGE 预留：Assigner 是天然 JUDGE（JD 写 KPI），但完整闭环依赖
  N5 任务结果状态机（未实现）——本模块只承载 assigner_ref/kpi_ref
  字段与 TaskResultRef 结构，不做实际闭环；
- 退出：agent 自决（exit intent → 恢复 resume_phase）或预算回落
  阈值下；被打断的工作下一 tick 立即续上。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from my_team.models.memory import (
    EntryOrigin,
    EntryProvenance,
    MemoryEntry,
    make_skill_entry,
)

if TYPE_CHECKING:
    from my_team.memory_recall import RecallEngine
    from my_team.memory_store import AgentMemory
    from my_team.transaction import TransactionBuffer

# ---------------------------------------------------------------------------
# 记忆工具集（CONSOLIDATING 下授权集切换目标，SPEC §4.4）
# ---------------------------------------------------------------------------

# 整理模式全开的记忆工具集；平时可用子集由初始授予集配置决定
# （_BASE_GRANT_TOOLS ∪ config.tools ∪ 本集合，见 simulation._initialize）
MEMORY_TOOL_NAMES: frozenset[str] = frozenset({
    "memory_fold",      # 折叠版本链/注入片段为浓缩条目
    "memory_promote",   # 提炼为长期 skill 条目（可关联 task_id 作结果 provenance）
    "memory_edit",      # 编辑条目（新版本）
    "memory_retag",     # 维护触发器（memory_points，新版本）
    "memory_evict",     # 撤出（移出工作集）
    "memory_pin",       # 固定（并入可控查询词，防召回降级）
})


# ---------------------------------------------------------------------------
# hysteresis 配置（进 90% / 出 80%）
# ---------------------------------------------------------------------------


class ConsolidationConfig(BaseModel):
    """CONSOLIDATING 进出阈值（hysteresis，防连续 tick 抖动）。

    - enter_ratio：固定注入使用率 ≥ 此值 → 预算触发进入；
    - exit_ratio：使用率 < 此值 → 预算回落退出（agent 自决退出不受限）。
    """

    enter_ratio: float = Field(default=0.9, ge=0.0, le=1.0, description="进入阈值（90%）")
    exit_ratio: float = Field(default=0.8, ge=0.0, le=1.0, description="退出阈值（80%）")


# ---------------------------------------------------------------------------
# 结构化摘要（输出契约）
# ---------------------------------------------------------------------------


class ConsolidationSummary(BaseModel):
    """CONSOLIDATING 结构化摘要（除折叠/压缩外须含反思/经验/优化/链接）。

    JUDGE 预留（N5 任务结果状态机未实现，仅承载字段）：
    - assigner_ref：Assigner（天然 JUDGE，JD 写 KPI）引用；
    - kpi_ref：KPI/评判标准引用。
    """

    reflection_and_growth: str = Field(default="", description="反思与进步")
    lessons_learned: str = Field(default="", description="经验教训整理")
    process_optimization: str = Field(default="", description="流程优化与提炼")
    memory_links: list[str] = Field(
        default_factory=list,
        description="记忆之间建立链接（相关条目标题/触发词/id）",
    )
    # JUDGE 预留字段（接口预留，非 N5 完整闭环）
    assigner_ref: str = Field(default="", description="Assigner（JUDGE）引用，预留")
    kpi_ref: str = Field(default="", description="KPI/评判标准引用，预留")


class ConsolidationOutput(BaseModel):
    """CONSOLIDATING LLM 输出的解析结果（结构化摘要 + 自决退出标志）。"""

    summary: ConsolidationSummary | None = Field(
        default=None,
        description="结构化摘要（缺失 = 本轮未产出）",
    )
    exit_requested: bool = Field(
        default=False,
        description="agent 自决退出标志（exit: true）",
    )


# ---------------------------------------------------------------------------
# LLM 输出解析（确定性，fake_llm 可测）
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从文本中提取第一个可解析的 JSON 对象。

    优先取 ```json 围栏块；否则尝试全文 / 首尾大括号包夹的子串。
    """
    for block in _JSON_FENCE_RE.findall(text):
        try:
            obj = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    candidates = [text.strip()]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_consolidation_output(content: str) -> ConsolidationOutput:
    """解析 CONSOLIDATING LLM 输出：结构化摘要 + exit 标志。

    契约（CONSOLIDATION_DIRECTIVE 中定义）：
    ```json
    {"consolidation_summary": {"reflection_and_growth": "...", ...},
     "exit": true}
    ```
    摘要缺失或格式非法 → summary=None（不抛异常，容错降级）。
    """
    obj = _extract_json_object(content)
    if obj is None:
        return ConsolidationOutput()
    summary = None
    raw_summary = obj.get("consolidation_summary")
    if isinstance(raw_summary, dict):
        try:
            summary = ConsolidationSummary.model_validate(raw_summary)
        except Exception:  # noqa: BLE001 — 容错降级
            summary = None
    return ConsolidationOutput(
        summary=summary,
        exit_requested=bool(obj.get("exit", False)),
    )


def parse_consolidation_summary(content: str) -> ConsolidationSummary | None:
    """便捷入口：只取结构化摘要（无摘要返回 None）。"""
    return parse_consolidation_output(content).summary


def parse_consolidation_request(content: str) -> bool:
    """普通模式下 LLM 内容是否含**主动整理请求**标记（主动触发，不限于预算满）。

    标记形式：文本含 "memory_consolidate" / "consolidate memory"，或
    JSON {"memory_consolidate": true}。
    """
    lowered = content.lower()
    if "memory_consolidate" in lowered or "consolidate memory" in lowered:
        return True
    obj = _extract_json_object(content)
    if obj is None:
        return False
    return bool(obj.get("memory_consolidate", False))


# ---------------------------------------------------------------------------
# 进出判定（hysteresis）
# ---------------------------------------------------------------------------


class ConsolidationGate:
    """CONSOLIDATING 进出判定（hysteresis：进 90% / 出 80%）。

    只做判定（纯函数），相位迁移由调用方（simulation decide/act 写
    路径）执行——Observe 只读消费 pending_consolidation / 使用率。
    """

    def __init__(self, config: ConsolidationConfig | None = None) -> None:
        self._config = config or ConsolidationConfig()

    @property
    def config(self) -> ConsolidationConfig:
        return self._config

    def should_enter(
        self,
        *,
        pending_consolidation: bool = False,
        usage_ratio: float = 0.0,
        active_intent: bool = False,
    ) -> bool:
        """是否进入 CONSOLIDATING。

        ① 预算触发：pending_consolidation 标志（组装器置位，Observe
        只读）或固定注入使用率 ≥ enter_ratio（90%）；
        ② 主动触发：active_intent（agent 主动发起，不限于预算满）。
        """
        return active_intent or pending_consolidation or usage_ratio >= self._config.enter_ratio

    def should_exit(
        self,
        *,
        usage_ratio: float,
        self_decided: bool = False,
    ) -> bool:
        """是否退出 CONSOLIDATING：agent 自决（self_decided）或使用率 < exit_ratio（80%）。"""
        return self_decided or usage_ratio < self._config.exit_ratio


# ---------------------------------------------------------------------------
# 结构化摘要条目（provenance 记整理来源）
# ---------------------------------------------------------------------------


def make_summary_entry(
    summary: ConsolidationSummary,
    *,
    agent_id: str,
    tick: int,
) -> MemoryEntry:
    """把结构化摘要构造为 MemoryEntry（type=skill，provenance 记整理来源）。

    content（SkillContent.sop_text）承载反思/经验/流程优化/链接四要素
    （+ assigner_ref/kpi_ref 预留字段，非空时序列化）；memory_links 同时
    作为 applies_to 与触发器（memory_points），使摘要条目可被链接词召回。
    """
    lines = [
        f"反思与进步: {summary.reflection_and_growth}",
        f"经验教训: {summary.lessons_learned}",
        f"流程优化: {summary.process_optimization}",
    ]
    if summary.memory_links:
        lines.append(f"记忆链接: {', '.join(summary.memory_links)}")
    if summary.assigner_ref or summary.kpi_ref:
        lines.append(
            f"JUDGE 预留: assigner_ref={summary.assigner_ref or '-'}, "
            f"kpi_ref={summary.kpi_ref or '-'}"
        )
    provenance = EntryProvenance(
        origin=EntryOrigin.OWN,
        consolidation_origin=f"consolidating:{agent_id}:{tick}",
    )
    return make_skill_entry(
        title=f"整理摘要 tick{tick}",
        sop_text="\n".join(lines),
        applies_to=list(summary.memory_links),
        memory_points=list(summary.memory_links),
        provenance=provenance,
    )


def write_summary_entry(
    store: "AgentMemory",
    engine: "RecallEngine",
    summary: ConsolidationSummary,
    *,
    agent_id: str,
    tick: int,
    buffer: "TransactionBuffer",
) -> MemoryEntry:
    """把结构化摘要写入 AgentMemory（MEMORY_ENTRY_WRITE effect + 索引同步）。

    全部动作 = Journal effect（可审计可回滚，N4_MEMORY_INJECTION_DESIGN §5）。
    """
    entry = make_summary_entry(summary, agent_id=agent_id, tick=tick)
    store.put(entry, buffer)
    engine.sync_put(entry)
    return entry


# ---------------------------------------------------------------------------
# CONSOLIDATING 系统提示（LLM 输出契约）
# ---------------------------------------------------------------------------

CONSOLIDATION_DIRECTIVE = (
    "MEMORY CONSOLIDATION MODE (CONSOLIDATING).\n"
    "You are consolidating your memory. Your input is the full memory\n"
    "injection set; your job is to make it smaller AND better.\n"
    "Produce consolidation actions via the memory tools:\n"
    "memory_fold / memory_promote / memory_edit / memory_retag /\n"
    "memory_evict / memory_pin. All actions are journal effects.\n"
    "When done, append a JSON block:\n"
    "```json\n"
    '{"consolidation_summary": {"reflection_and_growth": "...", '
    '"lessons_learned": "...", "process_optimization": "...", '
    '"memory_links": ["..."]}, "exit": true}\n'
    "```\n"
    "The structured summary MUST include reflection and growth, lessons\n"
    "learned, process optimization, and links between memories. Set\n"
    "'exit': true to end the consolidation session and resume your\n"
    "interrupted work."
)
