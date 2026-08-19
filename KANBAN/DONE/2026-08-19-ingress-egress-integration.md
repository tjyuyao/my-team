---
kind: task
status: completed
phase: v0.10 边界
source: SPEC §8.1、§8.2；OI-005 §3、OI-006 §3
priority: high
---

# v0.10-9: Ingress/Egress 传输层与 Integration 注册（映射并入 v0.11 E1）

## 范围注记（2026-08-18 重划后）
本卡收敛为**方向中立的传输层**：可靠入站、去重、ack、Integration 注册、
出站 pending op。事件入内核后的**映射前门**（`IngressEvent →
ProcessInstance`）属 v0.11 编排层 E1，本卡不再包含，也不按旧设计
（直接转 WakeEvent/TaskCreate/Record/Email）开工。见 SPEC §8.1。

## 目标
外部平台事件（消息、评价、订单、评论、数据回传）能可靠进入内核；
出站请求统一走 pending op；平台适配器作为 Integration 一等公民注册运行。

## 要求 / 规则
- `IngressEvent` 模型：source、external_id、event_type、occurred_at、
  payload、idempotency_key、priority。
- `IngressBuffer`：tick 之间写入，Ingest 阶段消费；`(source, external_id)`
  持久化去重；事件持久化成功后才 ack。
- **出站等待与唤醒（WAITING_FOR_EXTERNAL）**：Agent 发出外部请求后
  **复用既有的 wait/wake 协议**主动等待，而非轮询。经代码核查（agent_state.py
  TRANSITION_TABLE / scheduler.py / pending_ops.py / intent.py WaitForEventIntent），
  既有地基已经齐备：`PendingOperationRegistry` 生命周期
  `SUBMITTED→PENDING→COMPLETED/FAILED/CANCELLED/TIMED_OUT`（含 eligible_tick、
  deadline_tick、state_epoch 围栏）；event-driven scheduler 按 `WakeCondition`/
  `WakeupEvent` 匹配唤醒；`_enqueue_result_wake`（simulation.py）已把 op 完成
  投成 TOOL_RESULT wake event。
- **本卡只做接入，不发明机制**：新增 `WakeEventType.EXTERNAL_RESULT`、
  `WaitingState.WAITING_FOR_EXTERNAL`、`OpType.EXTERNAL_REQUEST` 三类，
  加一段 `_enqueue_result_wake` 分支；Egress op 走既有 PendingOperationRegistry。
  不做 Ingress 事件→任务的业务映射（留给 v0.11 E1）。
- **外站回执唤醒靠扩展界面规范（决策4）**：真实平台回执**必然不带内核 op_id**，
  只带平台自身的 `external_id`（评论 id/订单 id）。`external_id ⇄ op_id` 的翻译
  **平台相关，属插件/场景包代码，不进内核**。内核只定义**扩展界面规范**：
  Integration 声明一个**回执断言**（在回执 payload 的哪个字段取 external_id、
  经哪个映射得 op_id），消费端按该断言把回执翻译成 PendingOperation 命中。
  翻译实现由场景包的插件提供；**T9 只定义该扩展界面，不为假平台特设任何
  功能路径（假平台是测试设施，不影响设计决策）**。
- Ingest 阶段可唤醒相关 Agent（"有事件到达"），但不隐式决定下游对象；
  下游（流程实例化）由 v0.11 E1 的 `IngressEvent → ProcessInstance` 接管。
- `Integration` 注册：name、credential_ref、rate_limits、manifests
  （出站工具，**动态注册** per 决策2）、ingress_event_types、health_check、
  **回执断言（映射 external_id → op_id，per 决策4）**。
- **出站工具（EXTERNAL_IRREVERSIBLE）**：提供幂等键；**不做乐观回查工具
  （per 决策3）**——平台未必支持查询，事件驱动下 Agent 退 `WAITING_FOR_EXTERNAL`，
  "没消息=等待"，结果/超时事件唤醒。
- **限流 = 独立一道 provider 闸（per 决策1b，语义分清楚）**：现有 `executor_admission`
  （executor_registry.py）只管**内核执行器/并发容量**；`Integration.rate_limits`
  （外部平台配额）是**另一所有权维度**——新增**独立 `ProviderAdmission`**，在
  Publish 前串接：`放行 := executor_admitted ∧ provider_admitted`。不混入
  `admit()`；保证 SUBMITTED 背压语义与 executor 限流一致。
- 先用假平台适配器（脚本/webhook 模拟器）做集成测试。

## 产出
- IngressBuffer 与 Integration 注册中心。
- **出站 pending op**：复用 `PendingOperationRegistry` + 独立 `ProviderAdmission`
  （决策1b）限流，无乐观回查工具（决策3）。
- **外站等待接入**：新增 `WakeEventType.EXTERNAL_RESULT` /
  `WaitingState.WAITING_FOR_EXTERNAL` / `OpType.EXTERNAL_REQUEST`，完成事件
  经既有 wait/wake 路径唤醒 Agent。
- **出站工具动态注册**（决策2）：Integration 注册时按 `manifests` 现场注入
  EXTERNAL_IRREVERSIBLE 执行器 + ToolManifest，走既有 executor 路径。
- **扩展界面规范**（决策4）：Integration 回执断言，`external_id → op_id`
  翻译框架定义；真平台翻译实现归场景包插件（不在本卡）。
- **测试设施（非功能）**：假平台适配器（脚本/webhook 模拟器）+ 传输层
  集成测试，用于验证上述各接入与扩展界面；不构成功能交付、不影响决策。

## 共享状态机规范（2026-08-18 讨论细化）
出入站与邮件**共用"投递管道状态机"，不共用存储/模型**。

**既有事实（勿重构）：** 邮件已是两层串接状态机——
1. `OutboxStatus`（outbox.py）：`STAGED→COMMITTED→DISPATCHING→DISPATCHED/FAILED/DEAD`，管"可靠投递管道"。
2. `EmailStatus`（models/email.py）：`QUEUED→DELIVERED→READ`，管"收件箱生命周期"（Outbox 投递成功后才进入）。

**共享件（新增，一处定义两份复用）：**
- 统一管道状态枚举：从邮件 `OutboxStatus` 提炼，出入站 pending op 复用同一定义（SUBMITTED 家族 = COMMITTED/DISPATCHING 的泛化；ACKED = DISPATCHED/已确认；FAILED/DEAD 保留）。
- 统一迁移函数：`advance(pipe_state, event) -> new_state | Reject`，唯一合法转换表；邮件与出站共用。
- 各自**独立的行存储与数据模型**：邮件仍 `OutboxEntry`（to/subject/body/attachments），出站新增 `EgressRequest`（idempotency_key/status_query），**不得**塞 Optional 字段进同一 model。
- 重试/补偿策略**各自声明**：邮件上限 max_retries=3；出站 EXTERNAL_IRREVERSIBLE 带补偿工具撤回不可逆动作偏置，不进共享迁移函数。
- INVERT_CONTRACT：只共享"未 ACKED/未 DELIVERED 期间可撤"的前段；已确认后的差异化回滚（出站补偿 vs 邮件无真实回滚）不进共享件。

**边界红线：** 共享=枚举 + 迁移函数 + 撤销边界；不共享=表 / model / 重试 / 补偿。违反此红线的信号是某一侧被迫出现 `Optional[对方字段]`。

## WAITING_FOR_EXTERNAL 与 IDLE 语义（2026-08-18 代码核查定稿）
**结论：不另立 IDLE 等待机制——既有状态机已足够，只加外部接入。** 经 agent_state.py
转译表核查，进入 `IDLE` 只有四条明确迁移（均非"等待"），IDLE 与 wait 语义相反：
| 迁移 | 方法 | 含义 |
|---|---|---|
| `READY→IDLE` | `start()` | 上线但无事做 |
| `PROCESSING→IDLE` | `finish_processing()` | **本轮处理结束、无 in-flight 等待（含 ReAct 最终总结轮）** |
| `BLOCKED→IDLE` | `resolve_block()` | 阻塞解除回常态 |
| `FAILED→IDLE` | `recover()` | 失败重试成功回常态 |

- **"无 tool 调用 / 最终总结轮"就是 `PROCESSING→IDLE`**，不需要为总结造 wait。
- **固定结束规则（无可配项）**：Agent 若"发出外部请求后无事可做"，退入
  `WAITING_FOR_EXTERNAL`（保有唤醒订阅），而非 blanket IDLE。
- **持久化等待由既有 `AgentWaitState` + `WakeCondition` + `WaitForEventIntent` 承载**，
  外站完成事件若命中 pending wait → wake up；IDLE 与 `WAITING_FOR_*` 各守转译表，
  不复用、不合并。
- **退避由 `deadline_tick`/`timeout_expired()`（pending_ops.py 已有）兜底**，到期
  `WAITING_FOR_EXTERNAL → BLOCKED/FAILED`，杜绝静默滞留。


## 验收标准
- [x] 平台事件注入后被可靠持久化，下一 tick 唤醒相关 Agent（仅"事件到达"
  通知，不隐式创建任务）
- [x] 重复 `(source, external_id)` 跨重启只入站一次
- [x] 出站工具在限流时保持 SUBMITTED 背压（executor 容量 与 provider 配额
  各自触发均保持 SUBMITTED）
- [x] 事件未持久化前不 ack（可测试故障注入）
- [x] Agent 发出外站请求后退入 `WAITING_FOR_EXTERNAL`；外站回执经**回执断言**
  翻译 `external_id → op_id` 命中 pending wait → 下一 tick 唤醒该 Agent
- [x] 出站工具随 Integration 注册**动态注入**可用（决策2）
- [x] 外站超时经 `deadline_tick` 到期 → `WAITING_FOR_EXTERNAL` 退出为
  BLOCKED/FAILED，不静默滞留
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过

## 完成注记（2026-08-19）

实现要点（全部按定稿决策 1b/2/3/4 落地）：

- **新模块**：
  - `src/my_team/ingress.py` — `IngressEvent`（统一信封）+ `IngressBuffer`
    （`(source, external_id)` 持久化去重；未持久化不 ack；`snapshot/restore`
    辅助供持久化层）。
  - `src/my_team/integration.py` — `Integration`（SPEC §6.4 一等公民：
    rate_limits / manifests / ingress_event_types / health_check / 回执断言）
    + `IntegrationRegistry`（含 `admit()` provider 闸、`record_dispatched()`
    配额计数、`resolve_op_id()` 翻译入口）+ `ReceiptAssertion`（决策4 扩展
    界面：external_id_field + op_id_resolver，翻译实现归插件）。
- **决策1b（独立 provider 闸）**：`IntegrationRegistry.admit()` 与
  `ExecutorRegistry.admit()` 互不混入；`_phase_dispatch` 对外站工具先查
  provider 闸再查 executor 闸，`放行 := executor_admitted ∧ provider_admitted`；
  任一闸 retryable → 保持 SUBMITTED 背压；provider 无主 → 永久拒绝。
- **决策2（动态注册）**：`Simulation.register_integration()` 按 manifests
  现场注入 ToolManifest + UNTRUSTED_OUT_OF_PROCESS executor，出站工具走
  既有 executor admission + dispatch 路径。
- **决策3（纯事件等待）**：无乐观回查工具；外站 op 提交时 Agent 转入
  `WAITING_FOR_EXTERNAL`（`advance_to_waiting_external`），结果/超时经既有
  wait/wake 路径唤醒；`_enqueue_result_wake` 对外站 op 发 `EXTERNAL_RESULT`
  事件。新增枚举：`WakeEventType.EXTERNAL_RESULT`、
  `WaitingState.WAITING_FOR_EXTERNAL`、`ContinuationPhase.WAITING_FOR_EXTERNAL`、
  `AgentState.WAITING_FOR_EXTERNAL`（转译表/PAUSED resume 全链补齐）、
  `OpType.EXTERNAL_REQUEST`。
- **决策4（扩展界面翻译）**：Ingress 回执经 Integration 的 `ReceiptAssertion`
  （payload 字段取 external_id → 插件 resolver 得 op_id）命中 pending wait →
  `_consume_ingress` 完成 op 并唤醒；真平台翻译实现归场景包插件。
- **持久化**：`_collect_state`/`_restore_state` 增 `ingress` 组件（seen 集 +
  pending 事件），跨重启去重经 SimulationStore 生效。
- **测试** `tests/test_ingress_egress_integration.py`（11 个）：动态注册注入、
  未知出站工具永久拒绝、WAITING_FOR_EXTERNAL 进入、回执唤醒、超时退出、
  provider 限流背压、两因独立性、Ingress 去重/ack/跨重启（buffer 级 +
  SimulationStore 级）。
- **数据点**：全量 861 passed（850+11）；`mypy src/` clean（47 源文件）；
  `ruff` 通过；kanban_lint 0。
- **设计变更（2026-08-19）**：`IngressEvent.deadline_hint` 字段已删除。
  Ingress 仅消息入站点（传输层），deadline 属任务语义（Task/ProcessInstance
  层）；截止语义由 v0.11 E1 映射时在任务层承载，不在传输层预置。
