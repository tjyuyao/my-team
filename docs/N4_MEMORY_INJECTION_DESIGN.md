# N4 记忆与注入系统 — 设计定稿与任务拆解

> 2026-08-25 定稿（2026-08-25 二版：吸收 grill 结论）。
> 对应卡：`KANBAN/TODO/2026-08-25-memory-injection.md`。
> 依据：SPEC §3.6/§4.2–4.6/§5.1/§5.8/§6.4/§8.4。
> 核心立场（v2 更新）：
> - **外加载条目 = 投影不入库**（权限边界结构性成立）、**记忆写入 =
>   Journal effect**（可回滚可重建）；
> - **context_compiler 保留签名重写为三预算注入管线**（存量冲击最小）；
> - **CONSOLIDATING = continuation 相位 + 授权集切换**（零新机制），且
>   **不只压缩，更是反思+经验+链接**，Assigner 是天然 JUDGE；
> - **记忆分两层**：条目网络（精炼层）+ 原始 ReAct 记录（原始层，归
>   PrivateStore），recall/retrieve 路径区分；
> - **岗人交接不全量继承**（邮件/师徒，B 独立演进）；
> - **量力而行**：工具返回超模型剩余预算即上下文感知截断；
> - **重放降级**：「可重建」是 Journal 投影，非「确定性重放」不变量；
>   **DeterministicReplay 删除**；
> - **LLM 执行器归位并入 N4 作 N4-6**。

## 1. MemoryEntry schema（models/memory.py）

```python
MemoryEntryType: task | skill | tool | person          # Enum
TaskContent:   { notes, progress, decision_rationale }
SkillContent:  { sop_text, applies_to }
ToolContent:   { source, entry, capability_decl }      # 受限 python 模组
PersonContent: { profile, relations, preferences }

MemoryEntry:
  entry_id: uuid4
  type: MemoryEntryType
  title: str
  content: 判别联合（type 判别，构造即校验）
  memory_points: list[str]        # 触发器/索引，主动维护
  associated: list[uuid4]         # 唯一 id 通道（content 禁 uuid，schema 校验+测试）
  version: int (>=1)              # 不可变版本链（变更 = 新版本追加）
  provenance: EntryProvenance     # origin: own | injected；injected 记 injection_ref 快照
```

- **结果 provenance（v2 新增）**：`memory_promote` 可关联 `task_id`，把
  「这条 skill 在哪个任务里产生了什么结果（completed/failed/escalated/
  customer_rejected）」作为 provenance 带进条目。这样 skill 条目天然带
  结果证据，下次召回时能看到「这策略在 3 次退款里 2 次成功 1 次升级」，
  而不是一条裸规则。这是「犯错中改进」闭环的数据基础（§5 CONSOLIDATING
  + Assigner JUDGE）。

## 2. MemoryStore 与 PrivateStore（Agent 私有态，两层）

**归属：完全并入 Agent**（每 agent 一实例，非设备，不注册 Authority）。
**不拆 Position/Agent 两部分**——岗位无私有语义，经手物在设备侧。

Agent 私有态分两层：

| 层 | 内容 | 性质 | 检索路径 |
|---|---|---|---|
| **精炼层** MemoryStore | 条目网络（MemoryEntry 版本链） | 结构化、主动精炼 | 触发器召回（关键词/语义） |
| **原始层** PrivateStore | prompt/response 全文 append-only JSONL | 忠实、不精炼 | 全文检索（按时间/task_id 流式） |

> **原始层已拆分**（2026-08-25）：原始层独立成 `raw-transcript-layer`
> 卡，本设计文档只负责**精炼层**（MemoryStore + 召回 + 注入 + 整理）。
> 两层联测（条目召回 vs 全文检索不混）见该卡。

- **精炼层（MemoryStore）**：
  - `AgentMemory` 每 agent 一实例；版本链 + 触发器倒排索引 +
    RecallConfig（可控查询词是状态）；
  - **外加载条目 = 投影不入库**——`injection_for` 每次渲染时包装为只读
    MemoryEntry 视图（provenance.injection_ref），store 中不存在该
    entry_id ⇒ 「来源段不可改写」是**结构性保证**（不在 store = 不可写），
    非运行时检查；自有条目（own）完整读写；
  - **写入 = Journal effect**（INVERT 逆操作：追加版本撤销 = 移除该版本），
    回滚/重建与 agent 状态同路径。
- **原始层（PrivateStore）**：
  - prompt/response 全文 append-only conversation JSONL（§4.5 机制），
    是记忆的**忠实原始层**，不被摘要/折叠破坏；
  - 与精炼层**检索路径分开**：原始层走全文检索（`search_transcript`
    式，按时间/task_id 流式返回片段），精炼层走触发器召回；
  - 体积问题用定期归档解决（Owner 审批），不作为设计约束。

## 3. 召回引擎（recall.py）

- 触发源三路：上下文词（专注 task/收件箱等）∪ 可控查询词（状态）∪
  临时覆盖（memory_recall 产物，一次性）；
- 匹配：触发器关键词/子串先行（memory_points + title 倒排）；`RecallBackend`
  接口可插拔——向量化的对象是**触发器文本**，content 永不入索引；
  召回面 = 触发器列表（可审计）；
- **主动回忆 `memory_recall`**：IntentType.MEMORY_RECALL → **走 effect 而非
  pending op**（写入 recall_config.temp_overrides，一次性）；Act 阶段天然在
  Observe 之后 ⇒ **延迟 1 tick 是结构性的**；无外部副作用无需 op 生命周期；
- 合并组装：工作记忆 = 固定注入（p<10，单独预算不可超）∪ 触发注入
  （p≥10 命中）∪ 召回命中（三路词）∩ token 预算；同一 entity 多 position
  授予去重取最高 priority；输出有序注入布局（source_tag/priority_class/
  detail_level）。

## 4. 注入组装器（context_compiler 重写）

- **保留类名 `ContextCompiler` 与 `compile()` 签名**（存量测试与
  _phase_observe 接线最小冲击）；`DEFAULT_POLICIES`/role 三档废弃；
- **三预算布局**：① 固定注入（p<10，fixed_memory_tokens 单独预算不可超
  可配置；超限 → 置整理模式触发标志）；② 召回注入（≥10 命中 + 召回命中，
  top-k + 详细度降级 full/summary/title-only）；③ 观察上下文（emails/
  tasks/KB/locks 等原 section 内容，剩余预算）；
- **来源段与不可覆盖**：`[POSITION_JD]`/`[POLICY]`/`[SKILL_INSTRUCTION]`/
  `[UNTRUSTED_CUSTOMER_CONTENT]` 等（来自 InjectionDecl.source_tag）；
  POLICY/[POSITION_JD] 最先且不可被覆盖；客户内容永不作系统指令（§8.4）；
- **版本戳入 Journal**：编译后写 `MEMORY_INJECTION_STAMP`（布局引用 +
  顺序 + 详细度 + 版本戳）；AgentObservation 增 `memory_injection` 字段
  （emails/task_states 等既有字段保持兼容）；
- **prompt_templates 拆分**：render_tool_definitions（保留，归工具面）/
  parse_llm_response（保留，归 LLM 执行器侧）/ render_system_prompt
  （收敛为"注入布局 → 最终 messages"纯排版，role 参数保留签名停止语义）。

## 5. 整理模式 CONSOLIDATING（v2 扩展）

**不只压缩，更是「反思与进步」**——取代 harness 的固定总结提示词：

- **触发**：① 组装器检测固定预算使用率 > 90%（只读标志
  pending_consolidation，Observe 无副作用）；② **agent 主动发起**（不
  限于预算满）。相位迁移在 decide/act（写路径）；
- `ContinuationPhase.CONSOLIDATING` + `resume_phase` 字段（退出后下 tick
  立即续上被打断工作）；
- 工具面收窄：CONSOLIDATING 下授权集切换为记忆工具集（memory_fold/
  promote/edit/retag/evict/pin）——N1b 后授权集本就是动态求值，零新机制；
- 输入 = 完整注入集（目的就是变小）；输出 = 整理动作序列 + **结构化摘要**
  ——除折叠/压缩外，**须含：反思与进步、经验教训整理、流程优化与提炼、
  记忆之间建立链接**；全部动作 = Journal effect；
- **结果评估（JUDGE）来自 Assigner**：任务有 assigner/assignee 设计
  （§5.8/§6.1），**Assigner 是天然的 JUDGE 提供方**，JD 里明确写
  KPI/评判标准；闭环 = 任务结果 → Assigner 评判 → CONSOLIDATING 反思
  提炼 → 更新 skill（带结果 provenance，见 §1）。Authority 非组织架构时，
  反馈须在业务层定义（等价于 JD）；
- 退出：agent 自决（exit → 恢复 resume_phase）或预算回落阈值下；
  **hysteresis**（进 90%/出 80%）防连续 tick 抖动。

## 6. 注入状态空间可重建（§3.6，重放降级）

- **三类 effect 入 Journal**：MEMORY_RECALL_CONFIG（策略调整）/
  MEMORY_RECALL（主动回忆）/ MEMORY_ENTRY_WRITE、EVICT、FOLD（条目管理）；
- **布局 stamp**：每 tick 每 agent 写 MEMORY_INJECTION_STAMP（紧凑格式：
  entry 引用列表 + 版本戳 = 版本元组/content hash，非内容快照）；
  Journal 通道：N1c 世界记忆设备落地前走现有 TickJournal/audit，N6 随迁；
- **重建函数 + 测试**：`reconstruct_injection(journal, agent_id, tick)`
  —— 断言"Journal 重建的注入序列 == 运行时实际注入序列"（§11 验收不变
  量直接落位）；
- **重放降级（v2）**：「可重建」是 append-only Journal 的**投影**（谁
  需要谁投影），**不是「确定性重放」这一设计不变量/保证**。现实世界不可
  两次踏入同一条河流——My-Team 记录（账本），不承诺重放（时间机）。
  派生视图（审计/对账/重放/恢复/KPI）暂缓，见 OPEN_ISSUE
  journal-projections；
- **DeterministicReplay 删除**：零使用者 + 快照式与 effect 式投影冲突；
  随迁删除 test_reliability 7 测试；RetryManager/TimeoutChecker/CrashGuard
  保留。

## 7. 岗人交接（不全量继承，v2 新增）

换岗时 A 的技能**不自动全量克隆**给 B（技能池爆炸是前车之鉴）：

- A 通过**邮件交接**筛选的技能，或作为 B 的**上司/师傅**在协作中指导；
- B 据自身情况**吸收并独立演进**（参考人类社会的真实解决方案）；
- 组织学习经「晋升 = 从 agent 态发布为组织资产」的**显式路径**，不靠
  私有记忆克隆；
- 实现上是 N2（岗人分离）语义的自然结果，N4 只需保证「晋升」接口存在，
  不做自动继承。

## 8. 量力而行（v2 新增，§3.8）

单次工具调用返回不得超过模型剩余处理能力：

- 上下文感知截断（按实时剩余 token 预算，非静态上限），截断记 Journal
  事件供预警；
- 无法一次处理多任务就让需求排队，不硬撑——「宁可排队、不可丢失」在
  上下文维度的延伸；
- 这是 LLM 执行器（N4-6）与注入组装器（N4-3）的共同责任：组装器算好
  剩余预算，执行器在边界处截断工具返回。

## 9. LLM 执行器归位（N4-6，并入 N4）

- dispatcher 构造参数 `simulation` → `registry: PendingOperationRegistry`
  （去 `_sim._pending_ops`/`_operations` 双重私有耦合，走公共接口）；
- fake_llm 实现 `LLMProvider` 协议（gateway v0.12 只换注册不换面）；
  advance 走公共接口，保留脚本化响应 + latency_ticks 回放语义；
- 归属 Agent 引擎（§4.6）；规模小且与主动回忆同语境（异步路径），并入
  N4 作子任务；若排期紧可拆独立小卡并行（零依赖）。

## 10. 任务拆解

| 子任务 | 范围 | 验收要点 | 依赖 | 并行 |
|---|---|---|---|---|
| N4-1 记忆模型与存储 | MemoryEntry schema（含结果 provenance）+ AgentMemory + 记忆写 effect（含 INVERT） | schema 校验（type-content 一致、content 禁 uuid）、版本链、effect 可回滚、外加载不在 store | 无 | 与 N4-6 并行 |
| N4-2 召回引擎 | 触发器索引 + KeywordRecallBackend + RecallBackend 接口 + 可控查询词 + MEMORY_RECALL intent | 关键词命中 top-k、可控查询词持久、主动回忆 1 tick、召回面=触发器列表 | N4-1 | 与 N4-6 并行 |
| N4-3 注入组装器重写 | 三预算注入管线 + 来源段 + 布局 + stamp + prompt_templates 拆分 + role 停止消费 + 量力而行截断 | `[POLICY]`/`[POSITION_JD]` 不可覆盖（测试）、固定预算不可超、stamp 入 Journal、工具返回超预算截断 | N4-1+2 接口 | 与 N4-4 并行 |
| N4-4 整理模式 | CONSOLIDATING 相位 + resume_phase + 触发接线（预算+主动）+ 记忆工具集 handler + 结构化摘要（反思/经验/链接）+ Assigner JUDGE 接线 + 自决退出 + hysteresis | 超预算触发、主动触发、工具面收窄、动作入 Journal、退出后续上、输出含反思/经验/链接（测试） | N4-1 + N4-3 触发接口 | 与 N4-3/5 并行 |
| N4-5 注入可重建 | 三类 effect + stamp + reconstruct_injection + 重建测试 + 删 DeterministicReplay | 注入序列可从 Journal 重建（确定性测试）；死代码移除 | N4-3 | 与 N4-4 并行 |
| N4-6 LLM 执行器归位 | dispatcher 注入 registry + fake_llm 协议化 + advance 走公共接口 + 上下文感知截断 | 无 `_operations` 直接访问（grep 断言）、fake/gateway 同协议、全量绿 | 无 | **全程并行** |

> **N4-7 原始 ReAct 记录层已拆分为独立卡** `raw-transcript-layer`
> （2026-08-25），不在本卡子任务内。

**主干串行链**：N4-1 → N4-2 → N4-3 → N4-5；N4-4/N4-6 并行。每子任务后
全量回归。

## 11. 风险清单（要点）

1. **存量测试随迁**：test_context_compiler（281 行，role 三档断言重写）、
   test_llm_agent（render_system_prompt 输出变化）、test_reliability
   （DeterministicReplay 7 测试随删）、test_llm_dispatcher/e2e_async_llm
   （dispatcher 构造变化）；
2. **与 N3 衔接**：`[POSITION_JD]`/`[ORG_EDGE]`（priority=1）进固定预算，
   布局器最高优先不可覆盖；JD 动态变更 → 渲染取 injection_for 即时投影，
   下 tick 生效；设备注入内容无显式版本 → stamp 记 content hash；
3. **与 N1b 衔接**：AgentConfig.role/prompt_templates role 参数保留字段
   停止读取（兼容桥，N8 收尾拆除）；
4. **与 N5 衔接**：§6.4 快照戳与 §3.6 注入可重建是同一族机制两面（agent
   侧 stamp vs 业务侧版本绑定）；联测 = 快照戳 + 注入序列一起重建"当时
   它知道什么"；
5. **Journal 通道未定型**：N1c/N6 迁移（先走 TickJournal/audit）；派生
   视图投影层暂缓（journal-projections）；
6. **Assigner JUDGE 闭环**：Assigner 评判结果如何进入 CONSOLIDATING 输入，
   需要 N5 的任务结果状态机配合（completed/failed/escalated 标签）；
7. **原始层体积**：conversation JSONL 会持续增长，定期归档（Owner 审批）
   作为唯一减压手段，不设自动删除；
8. **hysteresis** 防抖动；**回滚一致性**（记忆 effect 逆操作 + snapshot
   matrix 扩展）；**注入安全不变量**（客户内容不作系统指令，验收测试显式
   覆盖）；**范围控制**（裁减顺序：memory_fold > 向量后端接口 > stamp
   细化 > 原始层检索）。

## 12. 卡修订建议（已并入卡）

N4 卡 v2 补：外加载条目 = 投影不入库、memory_recall 走 effect、重放降级
（投影非重放）、CONSOLIDATING 反思+Assigner JUDGE、岗人交接不全量继承、
量力而行截断；产出补 N4-6 LLM 执行器。**原始 ReAct 记录层拆分为
`raw-transcript-layer` 独立卡**。
