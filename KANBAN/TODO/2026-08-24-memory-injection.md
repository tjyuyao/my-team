---
kind: task
phase: v0.11 扩展表面
source: SPEC §5；三态收敛（2026-08-24）
priority: high
---

# 记忆与注入系统（条目/召回/工作记忆/整理模式）


## 目标
实现 SPEC §5 的记忆系统：长期记忆条目列表 + 工作记忆注入集 +
触发器召回 + 记忆整理模式 + 注入状态空间可重放。这是 v0.11 最大的
新实现块（Agent 引擎的"内心"）。

## 要求 / 规则
- MemoryEntry：`{entry_id uuid4, type(task|skill|tool|person),
  title, content(type-aware), memory_points[], associated[](uuid4),
  version}`；id 走 associated 不进 content；写入 = 系统自动沉淀 +
  agent 主动管理，皆 Journal effect；版本不可变链；
- 工作记忆 = **注入集**（预算内，直接影响下一次 LLM 请求）=
  召回(上下文词 ∪ 可控查询词 ∪ 临时覆盖) ∩ 预算；
- **授予注入分级（priority，N1/N2 联测）**：外加载条目（必然对应
  一条 `(position, entity_id)` 授予）带 priority——`< 10` 固定
  工作记忆（按序、持久有效；**单独预算、不可超，预算可配置**，
  JD 属此类）；`≥ 10` 经触发器召回；自有条目（agent 自写）完整
  读写权限，外加载条目受合理限制（来源段不可改写）；
- 可控查询词是状态（agent 可显示控制，属注入状态空间）；主动回忆
  = `memory_recall` intent（临时召回策略，延迟 1 tick，复用异步
  基建非阻塞）；
- 召回：触发器关键词/子串匹配先行，向量化同索引可插拔后端
  （**内容不向量化**，召回面 = 触发器列表可审计）；top-k 注入 +
  来源段标签（`[SKILL_INSTRUCTION]`/`[POLICY]`/`[POSITION_JD]`/
  `[UNTRUSTED_CUSTOMER_CONTENT]`）；
- 整理模式 CONSOLIDATING：预算超阈值进入（ContextCompiler 组装
  检测），工具面收窄为记忆工具集（memory_fold / promote / edit /
  retag / evict / pin），LLM 以完整注入集为输入，输出整理动作 +
  极短摘要，agent 自决退出，被打断的工作下一 tick 立即续上；
- 注入状态空间 S = (注入布局, 召回策略配置含可控查询词, 条目状态)；
  T 确定 + 三类 effect（策略调整/主动回忆/条目管理）入 Journal
  ⇒ 注入序列可重建（注入布局 + 版本戳入 Journal）；
- 记忆/注入机制 = Agent 引擎（数据 = agent 态）；org 对 agent 的
  唯一杠杆 = `[POSITION_JD]`（N2）。

## 产出
- 记忆条目存储（版本化）+ 记忆工具集；
- ContextCompiler 改造为注入管线（召回 + 预算 + 来源段）；
- CONSOLIDATING 相位（continuation/agent_state 扩展）+ 整理流程；
- 注入可重放（Journal 重建注入序列）。

## 验收标准
- [ ] 触发召回注入 top-k（预算内）；可控查询词持久影响召回
- [ ] 主动回忆延迟 1 tick 生效（非阻塞，复用异步基建）
- [ ] 超预算触发整理模式；整理动作入 Journal；退出后工作立即续上
- [ ] 注入布局 + 版本戳可从 Journal 重建（可重放，有测试）
- [ ] [POLICY]/[POSITION_JD] 不可被 skill/客户内容覆盖（有测试）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
