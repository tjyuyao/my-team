---
kind: task
phase: v0.11 agent-impl
source: SPEC §4（记忆系统）；三态收敛（2026-08-24）
priority: high
---

# 记忆与注入系统（条目/召回/工作记忆/整理模式）


## 目标
实现 SPEC §4 的记忆系统：长期记忆条目列表 + 工作记忆注入集 +
触发器召回 + 记忆整理模式 + 注入状态空间可重建（Journal 投影）。
这是 v0.11 最大的新实现块（Agent 引擎的"内心"）。

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
- 整理模式 CONSOLIDATING：**不只是压缩，更是「反思与进步」**。触发
  = 预算超阈值（必要）或 agent 主动发起（不限于预算满）；工具面收窄
  为记忆工具集（memory_fold / promote / edit / retag / evict / pin）；
  LLM 以完整注入集为输入，输出整理动作 + **结构化摘要**（须含反思、
  经验教训、流程优化、记忆链接，不只折叠）+ 极短摘要，agent 自决退出，
  被打断的工作下一 tick 立即续上（hysteresis 防抖动）；
- **结果评估（JUDGE）来自 Assigner**：任务有 assigner/assignee 设计，
  **Assigner 是天然的 JUDGE**，JD 里写 KPI/评判标准；「犯错中改进」
  闭环 = 任务结果 → Assigner 评判 → CONSOLIDATING 反思 → 更新 skill
  （带结果 provenance）；
- **原始 ReAct 记录层已拆分**（见 `raw-transcript-layer` 卡）：原始
  prompt/response 全文 append-only JSONL 归 PrivateStore，是记忆的
  **原始层**；本卡只做**精炼层**（条目网络）。两层检索路径分开，联测
  见 `raw-transcript-layer`。
- **岗人交接不全量继承**（向人类社会学习）：换岗时 A 的技能不自动
  克隆给 B；A 经邮件交接筛选技能，或作 B 的上司/师傅指导；B 吸收并
  独立演进；
- **量力而行**（§3.8）：单次工具返回超过模型剩余处理能力 → 上下文
  感知截断 + 记 Journal 预警；无法一次处理多任务就让需求排队；
- 注入状态空间 S = (注入布局, 召回策略配置含可控查询词, 条目状态)；
  T 确定 + 三类 effect（策略调整/主动回忆/条目管理）入 Journal
  ⇒ 注入序列可重建（注入布局 + 版本戳入 Journal；「可重建」是
  Journal 投影，非「确定性重放」不变量，见 OPEN_ISSUE
  journal-projections）；
- 记忆/注入机制 = Agent 引擎（数据 = agent 态）；org 对 agent 的
  唯一杠杆 = `[POSITION_JD]`（N2）。

## 产出
- 记忆条目存储（版本化）+ 记忆工具集；
- ContextCompiler 改造为注入管线（召回 + 预算 + 来源段）；
- CONSOLIDATING 相位（continuation/agent_state 扩展）+ 整理流程；
- 注入可重建（Journal 重建注入序列，投影非重放）；
- PrivateStore 原始 ReAct 记录层（append-only conversation JSONL）。

## 验收标准
- [ ] 触发召回注入 top-k（预算内）；可控查询词持久影响召回
- [ ] 主动回忆延迟 1 tick 生效（非阻塞，复用异步基建）
- [ ] 超预算触发整理模式；整理动作入 Journal；退出后工作立即续上
- [ ] 注入布局 + 版本戳可从 Journal 重建（投影非重放，有测试）
- [ ] CONSOLIDATING 输出含反思/经验/链接（不只压缩）；主动触发可用
- [ ] 原始 ReAct 记录 append-only 落 PrivateStore；条目层与原始层检索
      路径区分
- [ ] Assigner 评判结果能进入 skill 更新（带 provenance）
- [ ] [POLICY]/[POSITION_JD] 不可被 skill/客户内容覆盖（有测试）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过

## 设计定稿（2026-08-25）

详细设计见 `docs/N4_MEMORY_INJECTION_DESIGN.md`。关键决策：
- **外加载条目 = 投影不入库**（结构性防改写：不在 store = 不可写）；
  记忆写入 = Journal effect（可重放可回滚）；
- context_compiler **保留签名重写**为三预算注入管线（固定/召回/观察）+
  来源段 + MEMORY_INJECTION_STAMP；role 停止消费（字段保留）；
- memory_recall 走 **effect 而非 pending op**（Act 在 Observe 后 ⇒ 延迟
  1 tick 结构性成立）；
- CONSOLIDATING = continuation 相位 + 授权集切换 + hysteresis；
- **DeterministicReplay 删除**（零使用者 + 与 effect 式重放冲突）；
- **LLM 执行器归位并入 N4 作 N4-6**（dispatcher 去私有耦合 + fake_llm
  协议化）。

**子任务**：N4-1 模型与存储 → N4-2 召回 → N4-3 组装器重写 → N4-5 可重放
（主干串行）；N4-4 整理模式 / N4-6 LLM 执行器全程并行。
