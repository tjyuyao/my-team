# v0.11 代码迁移计划（50 文件全量三态审计）

> 2026-08-24。对 `src/my_team/` 全部源文件的自底向上审计（与 N1–N9
> 自顶向下规划互补）：每个现有文件逐一映射到三态（内核/设备/Agent），
> 判定迁移动作，核对卡片覆盖，暴露计划缺口。
> 权威设计见 SPEC §1.7/§2.1/§3/§4/§5；本文件是迁移执行清单。
>
> **更新注记（2026-08-25，grill 后）**：本文件是 08-24 审计快照，部分
> 结论已随 grill 更新——①「确定性重放」降级为「Journal 投影」，重放/
> 审计/对账/恢复/KPI 派生视图暂缓（OPEN_ISSUE journal-projections）；
> ②「RecordStore 删 ledger」暂缓（见 §4.1 裁决项 1）；③ Authority 从
> 「布线+裁决」改为「布线+注入，不裁决」，裁决下放设备（§3.5/§5.1）。
> 文中「重放」「删 ledger」字样按此理解，不再逐行改写。

## 0. 审计方法

- 49 个源文件 + 2 个 `__init__`，四组并行审计（内核执行域 12 / Agent
  引擎域 11 / 设备数据域 12 / 协议模型域 15）+ `simulation.py`（5597 行）
  人工专项 + `runtime.py`/`models/intent.py` 补审。
- 判定标准（SPEC v0.11 定稿）：
  - **内核**（纯逻辑，零业务数据，可带配置）：tick 十阶段、事务回滚、
    效果级策略求值、ACL 求值（position 两层 Grant）、执行真理
    （执行器分级/沙箱/锁原语/OperationPolicy 机制）、认知真理
    （注入可重放）、Human UI 框架、闭包不变量、Journal 写入/回滚逻辑。
  - **设备**（数据+读写工具+ACL+锁，动态向 Authority 注册受控 uuid，
    依赖用接口声明）：SharedKB/Record/Asset/Credential/邮箱/Task/
    组织架构/世界记忆（Journal 持久化与查询）/配置/外部世界（Ingress/
    Egress/Integration/MCP）/Authority（特殊设备）。
  - **Agent**（内心/头脑/双手）：身份、私密记忆（条目/注入/整理）、
    continuation、私有工作区（非设备）、LLM 执行器（供应商是内部结构；
    fake_llm 保留回放后端）。
- 已废除旧模型：ROOT_TOOLS/MANAGER_TOOLS/WORKER_TOOLS、按名字的
  `agent.tools`（ToolContext.allowed_tools/ToolRegistry）、
  AgentConfig 的 role/tools/parent-children、独立工具白名单。
- **N1 已拆分为 N1a/N1b/N1c**（2026-08-24）：N1a 设备协议与 Authority
  （纯新增地基）、N1b 白名单废除 + 工具契约（横切）、N1c 设备归位
  （存量适配）。下文中"覆盖卡 N1"指 N1 族，精确分工以三张卡为准。

## 1. 文件迁移总表

### 1.1 内核（纯逻辑）—— 22 文件

| 文件（行数） | 现状 | 迁移动作 | 覆盖卡 |
|---|---|---|---|
| simulation.py（5597） | 十阶段 + 全部接线 + 工具处理器 + 状态序列化 | **拆解**（见 §3 专项） | N1-N9 全部接线点 |
| tick_engine.py（255） | 时钟正确；模型层滞后（TickPhase 7 旧阶段、TickSnapshot 全量快照） | 适配：TickPhase→十阶段；TickSnapshot 按需化/删除（零使用者） | 缺口 → 并入 N1 |
| transaction.py（556） | 逆操作契约已对齐 §3.3 | 保留+适配：EffectType 与设备核对、锁拆分核对、outbox 逆操作接 N6 | N1/N6 |
| patch_ops.py（181） | 纯 diff 引擎 | 保留（落位 N1 工具清单） | 缺口 → N1 |
| budget.py（517） | 三作用域预算 | 拆解：LLM 限额归 Agent 引擎、外部限额归 Ingress 设备、内核留容量背压 | N1 |
| executor_registry.py（190） | 三级执行器已对齐 | 保留+适配：平台级 Admission 移 Integration 设备 | N1/N6 |
| sandbox_spec.py（233） | 声明式约束已对齐 | 保留（N7 扩 L2 边界字段） | N7 |
| sandbox_tools.py（462） | 宿主沙箱后端 | 保留（默认 SandboxBackend） | N7/v0.13 |
| python_worker.py（349） | L0/L1 受限执行 | 保留+适配：显式禁随机/时钟（N7 L1 对齐）；L2 待建 | N7 |
| audit.py（210） | AuditLog 已挂 Journal 但仍独立存储（双轨） | 适配：投影化（消除独立 `_entries`） | 缺口 → P1b9/统一 Journal |
| reliability.py（543） | 重试/超时/CrashGuard/DeterministicReplay（死代码） | 拆解：重试内核、FailureRecord 数据化、Replay 并入 N4、CrashGuard 接 N6 | N4/N6 |
| runtime.py（162） | 墙钟循环 | 保留（§2.2 已对齐） | — |
| control_plane.py | Human UI 操作台 | 适配：补审批端点 + 设备 UI 插件注册表 | N3 |
| models/activation.py（229） | 调度数据模型 | 保留+适配：WaitingState 与 agent_state 去重；ExecutionConfig 并入限额表 | 缺口 → N1/N8 |
| models/intent.py（251） | 12 种 Intent 契约 | 保留+适配：N4/N5 增补 intent（memory_recall/exit 等） | N4/N5 |
| authority.py（545，旧裁决） | 8 域裁决（同名异义！） | **改名** + 并入 N5 裁决引擎（Escalation 归 N5 归一） | N5 |
| tool_manifest.py | 工具契约 | 大改：6 新字段（device_id/capability/approval_policy/ingress/egress/compensation）；builtin 拆设备注册；allowlist 挪配置设备 | N1/N6 |
| tool_protocol.py | 请求/结果契约 | 保留+适配（from/to 双身份，见裁决项 4） | N1/N9 |
| models/llm.py | LLM 模型 | 保留（§4.6 ToolDefinition 自动生成目标） | N1 |
| calendar.py | 日历规则+判定 | 拆解：CronSpec/ScheduleRule 数据归设备、到期判定/RULE_ADVANCE 留内核 | 缺口 → N1/§7 |
| scheduler.py | 调度算法+容量 | 拆解：算法留内核 Schedule、max_active_agents 归配置设备 | 缺口 → N1/§3.8 |
| journal.py（部分） | 写入/回滚逻辑 | 写入/回滚留内核（契约 §3.2） | N1/N6 |

### 1.2 设备（数据+工具+ACL+锁）—— 15 文件

| 文件（行数） | 现状 | 迁移动作 | 覆盖卡 |
|---|---|---|---|
| shared_kb.py | KB 设备雏形 | 拆解：PermissionEngine 路径授权 → 逐条目 entity_id ACL（裁决项 2）；LockManager 锁实例留设备 | N1 |
| record_store.py | 记录存储 + 自带 ledger | 适配（主体符合）；**ledger vs Journal 投影冲突需裁决**（裁决项 1） | N1 |
| asset_store.py | 资产存储 | 适配：补持久化/ACL | N1 |
| credential_store.py | 引用式凭证 | 保留+适配 | N1 |
| mailbox.py | 邮箱系统 | 拆解：账号 agent_id → position（经手物归岗） | N1/N3 |
| task_tree.py | 任务树 | 拆解：position/Authority 接点（细粒度按 position 求值） | N1/N5 |
| models/task.py | 任务模型 | 适配：归 Task 设备（N5 接点字段） | N1/N5 |
| models/email.py | 邮件模型 | 适配：补 HUMAN_APPROVAL_REQUEST | N1/N3/N5 |
| journal.py（部分） | 内存 TickJournal | 持久化/查询归世界记忆设备（SQLite 落 N6） | N1/N6 |
| outbox.py | 出站队列 | 状态机留内核、投影/Egress 归设备；entry_id 去 uuid4 | N6 |
| pending_ops.py | op 注册表 | 内核逻辑保留、恢复/对账归设备（restore_seen_requests 雏形） | N6 |
| ingress.py | 入站缓冲 | 保留（外部世界设备 §5.11，结构已对齐） | T17/N1 |
| integration.py | 平台适配器 | 保留+适配（补 webhook_endpoint；rate_limit 背压归此） | T17/N1 |
| models/agent.py | AgentConfig（含旧字段） | **重构**：删 role/tools/parent-children/SharedKBPermission；PoolConfig 保留 | N2/N3 |
| models/continuation.py | Agent 私密态 | 保留（Agent 态） | N4/N6 |

### 1.3 Agent（内心/头脑/双手）—— 10 文件

| 文件（行数） | 现状 | 迁移动作 | 覆盖卡 |
|---|---|---|---|
| agent_runtime.py（851） | 协议 + 白名单 + rule-based 三档 Agent | 拆解：白名单删除；协议骨架归 Agent 引擎；role 三档废弃；HumanWorkerRuntime 归 N3/N5 | N1/N2/N3/N5 |
| agent_state.py（445） | 状态机 + AuditLog | 适配：状态机归 Agent；AuditLog 去向定（统一 Journal）；WaitingState 去重 | N4 |
| agent_tree.py（367） | 静态组织树（旧模型） | **删除**；树不变量移 N8 静态校验器（边语义四条） | N2/N3/N8 |
| llm_agent.py（119） | LLM Agent | 适配：去 role/allowed_tools；prompt 组装让位 N4 注入集 | N2/N4 |
| llm_dispatcher.py（184） | 后台轮询线程 | 保留+适配：归 Agent 引擎 LLM 执行器；去 simulation 私有成员耦合 | 缺口 → LLM 执行器归位 |
| llm_gateway.py（234） | LLM 网关 | 保留+适配（v0.12 接 CredentialStore；v0.11 留 fake 路径） | v0.12 |
| fake_llm.py（155） | 确定性回放后端 | 保留（走 pending_ops 接口，不注册设备） | N4 |
| context_compiler.py（404） | role 三档观察组装 | **重写**：N4 注入器（来源段/布局入 Journal）；role 策略废弃 | N4 |
| prompt_templates.py（122） | 模板渲染 + 工具定义 | 适配/重写：去 allowed_tools；来源段结构化 | N4/T18 |
| private_store.py（175） | 私有工作区 | 保留（Agent 内部机制，**非设备**，不注册 Authority） | N4 |

### 1.4 导出/杂项

| 文件 | 动作 |
|---|---|
| `__init__.py`（根）/ `models/__init__.py` | 随迁移更新导出面（无独立动作） |

## 2. simulation.py 专项（5597 行，迁移主战场）

| 现状区块 | 目标去向 |
|---|---|
| 十阶段 `_phase_*`（run_tick/ingest/schedule/observe/decide/validate/act/commit/publish/dispatch/audit） | **内核 K**（保留，§3.1） |
| `_register_tool_handlers`（~830 行工具处理器注册） | **各设备工具面**：按设备域拆出（KB/Record/Asset/Mail/Task/私密区/凭证/集成），经 ToolPlugin API 向 Authority 注册 |
| `_collect_state`/`_restore_state`（~350 行全量状态序列化） | **世界记忆设备**（Journal 重放模型）替代；过渡期保留（N6 依赖） |
| `_plugin_handles`（v0.10 T7） | **设备句柄注入**（已是雏形，直接演进） |
| `register_tool` | **ToolPlugin API**（§5.1，加 device_id/capability 注册到 Authority） |
| 白名单接线 4 处（L3640 ToolContext.allowed_tools / L4007、L4053 按名检查 / L4766 dispatch 上下文） | **两层 Grant 求值**（∃position：Grant(agent,position) ∧ Grant(position,entity_id) ∧ 锁） |
| `_select_pool_child`/`_dispatch_deferred_pools`（WorkerPool） | §7.3 语义（pool = service manager 节点） |
| `_check_calendar`/`_check_deadlines` | 内核调度/期限逻辑（数据归设备） |
| `AgentRuntimeState`（L207-294） | Agent 引擎（continuation/等待语义） |

**迁移顺序建议**（与 N1a/N1b/N1c 实施对齐）：
1. 先立设备协议与 Authority（新代码，不动旧路径）→ 2. 白名单废除
   （simulation 4 接线点 + agent_runtime/llm_agent/prompt_templates）→
   3. 工具处理器按域拆设备 → 4. 状态序列化/持久化归位（N6 衔接）→
   5. 其余（日历/WorkerPool/AuditLog 投影）。

## 3. 测试影响面（迁移冲击评估）

测试→模块耦合 Top（`grep ^from my_team` 计数）：
- `agent_tree`(54)、`simulation`(49)、`agent_runtime`(39)、
  `models.intent`(28)、`audit`(22)、`transaction`(20)、
  `models.continuation`(20)、`models.activation`(20)、`agent_state`(18)、
  `pending_ops`(16)、`tool_manifest`(14)、`shared_kb`(12)。
- 白名单废除直接命中：`test_agent_runtime`、`test_llm_agent`、
  `test_task_cancellation`（ROOT/MANAGER/WORKER_TOOLS 引用）。
- `tests/test_authority.py`（444 行 32 测试）随 authority.py 改名迁移（N5）。

## 4. 计划缺口清单

### 4.1 高优裁决项（已定案，2026-08-24 Owner 拍板）

1. **RecordStore ledger（已定案：设备不维护账本；2026-08-25 暂缓）**：
   设备只持当前状态（effect 应用直接改状态），投影/重建源唯一 = Journal；
   RecordStore 删 ledger 与「重放一致性由重放测试把关」**暂缓**（见
   OPEN_ISSUE journal-projections）；RecordStore 现持当前状态，回滚维持
   invert_data + ledger_ids 现状。落位 N6 的仅是 Journal 持久化落地。
2. **SharedKB 权限模型（已定案：注册即声明注入内容）**：注册受控
   uuid 时设备同时声明"授予生效后注入记忆的 content"（引导 Agent
   使用，如页面权限说明——注入记忆非数据全量）；授权查 Authority，
   注入内容的解释权在设备内部；PermissionEngine 的匹配逻辑可保留为
   设备内部实现，授权数据源换为两层 Grant。落位 SPEC §5.1 + N1a。
3. **scheduler/WorkerPool 容量语义（已定案）**：容量参数归配置设备
   （N1a）；WorkerPool service 节点语义归 N2/N3（kind=service +
   组织形态）；plan 依赖图已更新。
4. **身份闭包（已定案：v0.11 现在就做）**：身份绑定 = 内核构造
   ToolContext/ToolRequest（agent_id + position_ref）；设备工具把
   上下文身份落为数据字段（from/assignee）是设备职责，非内核注入；
   Agent 写作范围无身份字段（结构性防伪）。落位 SPEC §3.5/§9 +
   N5（审批审计）/N9（不变量）。P1 backlog#7 提升为 v0.11 范围。

### 4.2 高优缺口（卡内消化即可）

5. **LLM 执行器族无卡**（gateway v0.12 / dispatcher / fake_llm）：
   归 Agent 引擎 §4.6。建议 v0.11 内：dispatcher 去 simulation 私有
   耦合走 pending_ops 接口；fake_llm 接口化（保留回放后端）；
   gateway v0.12 前置拆解。补一张"LLM 执行器归位"卡或并入 N4。
6. **注入组装职责真空**：context_compiler 重写 + prompt_templates 去
   allowed_tools 后，"谁组装最终注入布局、写 Journal"无卡点名 →
   N4 卡显式声明迁移目标文件。
7. **AgentConfig 旧字段跨文件耦合**：role/tools/parent_id/children 穿透
   6 个文件（models/agent、agent_tree、llm_agent、context_compiler、
   prompt_templates、simulation）→ N2 卡列出全部消费方清单。
8. **authority.py 改名落位**：裁决核心并入 N5 须改名（建议
   `authority_evaluation.py`，保留 8 域语义）；test_authority.py 随迁；
   N5 卡补改名动作。
9. **邮箱设备化无卡点名**：mailbox.py + models/email.py 的 position
   迁移与 HUMAN_APPROVAL_REQUEST → N1/N3/N5 补。
10. **ToolManifest 新契约字段卡间未对齐**（device_id/capability/
    approval_policy/ingress/egress/compensation_tool 在 N1/N6 的分工）
    → N1 补字段清单、N6 补幂等/回查衔接。
11. **Control Plane↔HumanTask 审批接点接口未定义** → N3 补端点契约。

### 4.3 中低缺口（归 P1 backlog 或卡内附注）

12. tick 模型层对齐（TickPhase 十阶段、TickSnapshot 删）→ 并入 N1。
13. patch_ops 落位 N1 工具清单。
14. file_ops 审计双轨 / audit.py 投影化 / persistence 存储替换 →
    P1 backlog 9「统一 Journal」；N6 卡补存储层过渡注记。
15. reliability.DeterministicReplay 死代码 → 并入 N4（§3.6 雏形）或删。
16. budget 拆分外部限额侧 → N1/T17 落位。
17. executor 平台级 Admission 迁移 → N1/T17 落位。
18. reliability.FailureRecord 数据归属（RecordStore/Journal 投影）→ N6。
19. test_authority.py 迁移动作 → N5。
20. WakeCondition/AgentContinuation 与 N4 记忆注入衔接 → N4 附注。
21. private_store 非设备 → N4 附注一句。
22. WaitingState 双枚举去重（agent_state vs models/activation）→ N1/N8。
23. agent_state.AuditLog 去向（内核 Audit vs 世界记忆设备）→ 随统一 Journal。

## 5. 建议卡修订（汇总）

| 卡 | 修订 |
|---|---|
| 卡 | 修订 |
|---|---|
| N1a | 补：设备协议三条（不维护账本/身份落字段/注册即声明注入内容，SPEC §5.1 已同步）、配置设备（容量参数）、注入接线接口；裁决项 1/2 落点 |
| N1b | 补：simulation 清理清单（4 白名单接线点）、ToolManifest 6 字段清单、tick 模型层对齐、patch_ops 落位 |
| N1c | 补：budget 拆分外部侧、executor 平台 Admission、日历/调度数据归设备、世界记忆设备接口层、裁决项 3 落点 |
| N2 | 补：AgentConfig 旧字段消费方清单（6 文件）、agent_tree 删除与不变量移交 N8 |
| N3 | 补：mailbox 账号 position、Control Plane 审批端点 + UI 插件注册表、WorkerPool service manager 语义 |
| N4 | 补：迁移目标文件点名（context_compiler 重写、prompt_templates、private_store 非设备）、DeterministicReplay 并入或删、LLM 执行器归位（或新卡） |
| N5 | 补：authority.py 改名（authority_evaluation）+ test_authority.py 随迁、Escalation 归一确认 |
| N6 | 补：outbox entry_id 去 uuid4、FailureRecord 数据化、persistence 过渡角色、统一 Journal 投影化范围 |
| N8 | 补：agent_tree 树不变量（环/根/一致性）→ 边语义四条 + WaitingState 去重 |
| T17 | 补：platform 级 Admission（rate_limit/健康背压）归 Integration 设备、budget 外部限额 |
| v0.12 | 前置：llm_gateway 接 CredentialStore、身份闭包（from/to 强制注入） |

## 6. 迁移原则（执行时遵守）

- **一卡一域**：迁移按卡推进，每张卡闭包自己的文件与测试；
  白名单废除是横切动作，由 N1 牵头、N2/N4 协同，防半迁移。
- **过渡期策略**：旧路径（白名单/组织树/全量序列化）在替换落位前
  保留，替换后删除——不并行维护双制。
- **测试随迁**：每个文件迁移时同步迁移其测试（改名/改断言），
  全量 `uv run pytest -q` 保持绿（当前 1006 passed）。
- **lint 纪律**：kanban_lint 0；卡名日期前缀 = committer date。
