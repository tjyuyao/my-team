# N4 记忆与注入系统 — 设计定稿与任务拆解

> 2026-08-25 定稿。设计草案由设计 agent 产出，主 agent 审阅定案。
> 对应卡：`KANBAN/TODO/2026-08-24-memory-injection.md`。
> 依据：SPEC §3.6/§4.2–4.6/§5.1/§5.8/§6.4/§8.4。
> 核心立场：**外加载条目 = 投影不入库**（权限边界结构性成立）、**记忆
> 写入 = Journal effect**（可重放可回滚）、**context_compiler 保留签名
> 重写为三预算注入管线**（存量冲击最小）、**CONSOLIDATING = continuation
> 相位 + 授权集切换**（零新机制）、**DeterministicReplay 删除**、**LLM
> 执行器归位并入 N4 作 N4-6**。

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

## 2. MemoryStore（Agent 私密态，非设备）

- `AgentMemory` 每 agent 一实例（归属 Agent 引擎，不注册 Authority，与
  private_store 同为 §4.5 机制）；版本链 + 触发器倒排索引 + RecallConfig
  （可控查询词是状态）；
- **权限边界**：**外加载条目 = 投影不入库**——`injection_for` 输出每次
  渲染时包装为只读 MemoryEntry 视图（provenance.injection_ref），store
  中不存在该 entry_id ⇒ **"来源段不可改写"是结构性保证（不在 store =
  不可写），非运行时检查**；自有条目（own）完整读写；
- **写入 = Journal effect**（带 INVERT 逆操作：追加版本撤销 = 移除该
  版本），回滚/重放与 agent 状态回滚同路径；
- 与 private_store `memory/` 目录**不绑定**（目录保留为通用文件区）。

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

## 5. 整理模式 CONSOLIDATING

- 触发：组装器检测固定预算使用率 > 90%（只读标志 pending_consolidation，
  Observe 无副作用）；相位迁移在 decide/act（写路径）；
- `ContinuationPhase.CONSOLIDATING` + `resume_phase` 字段（退出后下 tick
  立即续上被打断工作）；
- 工具面收窄：CONSOLIDATING 下授权集切换为记忆工具集（memory_fold/
  promote/edit/retag/evict/pin）——N1b 后授权集本就是动态求值，零新机制；
- 输入 = 完整注入集（目的就是变小）；输出 = 整理动作序列 + 极短摘要；
  全部动作 = Journal effect；
- 退出：agent 自决（exit → 恢复 resume_phase）或预算回落阈值下；
  **hysteresis**（进 90%/出 80%）防连续 tick 抖动。

## 6. 注入状态空间可重放（§3.6）

- **三类 effect 入 Journal**：MEMORY_RECALL_CONFIG（策略调整）/
  MEMORY_RECALL（主动回忆）/ MEMORY_ENTRY_WRITE、EVICT、FOLD（条目管理）；
- **布局 stamp**：每 tick 每 agent 写 MEMORY_INJECTION_STAMP（紧凑格式：
  entry 引用列表 + 版本戳 = 版本元组/content hash，非内容快照）；
  Journal 通道：N1c 世界记忆设备落地前走现有 TickJournal/audit，N6 随迁；
- **重建函数 + 测试**：`reconstruct_injection(journal, agent_id, tick)`
  —— 断言"Journal 重建的注入序列 == 运行时实际注入序列"（§11 验收不变
  量直接落位）；
- **DeterministicReplay 删除**：零使用者 + 快照式与 effect 式重放冲突；
  随迁删除 test_reliability 7 测试；RetryManager/TimeoutChecker/CrashGuard
  保留。

## 7. LLM 执行器归位（N4-6，并入 N4）

- dispatcher 构造参数 `simulation` → `registry: PendingOperationRegistry`
  （去 `_sim._pending_ops`/`_operations` 双重私有耦合，走公共接口）；
- fake_llm 实现 `LLMProvider` 协议（gateway v0.12 只换注册不换面）；
  advance 走公共接口，保留脚本化响应 + latency_ticks 回放语义；
- 归属 Agent 引擎（§4.6）；规模小且与主动回忆同语境（异步路径），并入
  N4 作子任务；若排期紧可拆独立小卡并行（零依赖）。

## 8. 任务拆解

| 子任务 | 范围 | 验收要点 | 依赖 | 并行 |
|---|---|---|---|---|
| N4-1 记忆模型与存储 | MemoryEntry schema + AgentMemory + 记忆写 effect（含 INVERT） | schema 校验（type-content 一致、content 禁 uuid）、版本链、effect 可回滚、外加载不在 store | 无 | 与 N4-6 并行 |
| N4-2 召回引擎 | 触发器索引 + KeywordRecallBackend + RecallBackend 接口 + 可控查询词 + MEMORY_RECALL intent | 关键词命中 top-k、可控查询词持久、主动回忆 1 tick、召回面=触发器列表 | N4-1 | 与 N4-6 并行 |
| N4-3 注入组装器重写 | 三预算注入管线 + 来源段 + 布局 + stamp + prompt_templates 拆分 + role 停止消费 | `[POLICY]`/`[POSITION_JD]` 不可覆盖（测试）、固定预算不可超、stamp 入 Journal | N4-1+2 接口 | 与 N4-4 并行 |
| N4-4 整理模式 | CONSOLIDATING 相位 + resume_phase + 触发接线 + 记忆工具集 handler + 自决退出 + hysteresis | 超预算触发、工具面收窄、动作入 Journal、退出后续上（测试） | N4-1 + N4-3 触发接口 | 与 N4-3/5 并行 |
| N4-5 注入可重放 | 三类 effect + stamp + reconstruct_injection + 重放测试 + 删 DeterministicReplay | 注入序列可从 Journal 重建（确定性测试）；死代码移除 | N4-3 | 与 N4-4 并行 |
| N4-6 LLM 执行器归位 | dispatcher 注入 registry + fake_llm 协议化 + advance 走公共接口 | 无 `_operations` 直接访问（grep 断言）、fake/gateway 同协议、全量绿 | 无 | **全程并行** |

**主干串行链**：N4-1 → N4-2 → N4-3 → N4-5；N4-4/N4-6 并行。每子任务后
全量回归（基线 1143 passed）。

## 9. 风险清单（要点）

1. **存量测试随迁**：test_context_compiler（281 行，role 三档断言重写）、
   test_llm_agent（render_system_prompt 输出变化）、test_reliability
   （DeterministicReplay 7 测试随删）、test_llm_dispatcher/e2e_async_llm
   （dispatcher 构造变化）；
2. **与 N3 衔接**：`[POSITION_JD]`/`[ORG_EDGE]`（priority=1）进固定预算，
   布局器最高优先不可覆盖；JD 动态变更 → 渲染取 injection_for 即时投影，
   下 tick 生效；设备注入内容无显式版本 → stamp 记 content hash；
3. **与 N1b 衔接**：AgentConfig.role/prompt_templates role 参数保留字段
   停止读取（兼容桥，N8 收尾拆除）；
4. **与 N5 衔接**：§6.4 快照戳与 §3.6 注入可重放是同一族机制两面（agent
   侧 stamp vs 业务侧版本绑定）；联测 = 快照戳 + 注入序列一起重建"当时
   它知道什么"；
5. **Journal 通道未定型**：N1c/N6 迁移（先走 TickJournal/audit）；
6. **hysteresis** 防抖动；**回滚一致性**（记忆 effect 逆操作 + snapshot
   matrix 扩展）；**注入安全不变量**（客户内容不作系统指令，验收测试显式
   覆盖）；**范围控制**（裁减顺序：memory_fold > 向量后端接口 > stamp 细化）。

## 10. 卡修订建议（已并入卡）

N4 卡补三处：外加载条目 = 投影不入库（权限结构性）、memory_recall 走
effect 而非 pending op、DeterministicReplay 删除；产出补 N4-6 子任务
（迁移文档缺口 5 取"并入 N4"）。
