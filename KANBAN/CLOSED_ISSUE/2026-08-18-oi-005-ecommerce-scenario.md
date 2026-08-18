---
kind: issue
status: closed
source: 应用场景三：电商平台管理（智能客服 / 恶评检测 / 多平台接入 / 进销存）
priority: high
---

# OI-005: 场景驱动的架构补充 — 电商平台管理

**Opened:** 2026-08-17（在 OI-004 基础上的场景扩展分析）
**Status:** CONVERTED — 已拆分为 TODO（v0.10 边界能力）
**Converted to TODO:** 2026-08-17-ingress-egress-integration.md, 2026-08-17-record-and-asset-store.md, 2026-08-17-scheduler-calendar-pool.md, 2026-08-17-approval-and-human-worker.md

---

## 0. 结论摘要

电商场景是对当前架构的一次很好的"压力测试"。它比软件开发公司和
小说工作室更**事件驱动、更外部化、更结构化**，能验证内核抽象是否
真正通用。

结论分两层：

1. **好消息**：tick 事务内核、pending op、state epoch、outbox 这些
   机制对电商是合适的——库存账本、跨平台幂等写入、SLA 超时处理
   恰好需要它们。
2. **坏消息**：电商场景暴露了三个当前架构完全没有的缺口——
   **入站事件总线（Ingress）**、**结构化记录存储（RecordStore）**、
   **真实的人类审批流（ApprovalGate）**。再加上 OI-004 已指出的
   Runtime 与 ContextCompiler 缺口，电商场景一个都跑不起来。

因此，建议把电商场景列为**架构通用性验收场景**，而不是等到软件/
小说场景之后再"打补丁"。

---

## 1. 四个子场景 → 架构需求映射

### 1.1 智能客服

现实流程：客户在淘宝/京东/抖音/微信等多平台发消息 → 平台 webhook
推送 → 系统建 ticket → CS Agent 读取上下文（订单、商品、历史
会话、话术）→ 草拟回复 → 必要时人工审批 → 平台 API 回复 → 记录。

对架构的需求：

- **入站事件（Ingress）**：当前内核只认识内部 Email 和 pending op
  完成；没有"外部平台事件"入口。需要 `IngressEvent` +
  `IngressBuffer`：外部适配器在 tick 之间写入事件，Ingest 阶段
  统一消费、按平台时间排序、按外部 event_id 去重（webhook 会
  重发）。
- **会话与 ticket**：客户会话是跨 tick 的持久对象，不能只靠
  AgentContinuation（会丢、不能换人）。需要 `Ticket`/`Conversation`
  记录类型，绑定平台、客户、订单、当前负责 Agent、SLA deadline。
- **技能路由 / 池路由**：`DelegateIntent` 目前只能委派给指定的
  直接子 Agent。多个 CS 坐席时，需要委派到 `WorkerPool`（按
  负载/技能路由），而不是点名。否则 Manager 每次都要自己选人，
  且无法水平扩展。
- **SLA 调度**：客服消息有响应超时。当前调度器按 agent_id 排序
  就绪集，没有按 deadline/priority 排队；`DEADLINE_APPROACHING`
  事件枚举存在但无人生成。需要 `SlaPolicy`（deadline_tick +
  priority）驱动调度与升级。
- **人工升级**：高情绪、高金额、疑难件要升级给人类。这正是
  "用户作为 Worker/审批者"的场景。

### 1.2 恶评检测

现实流程：评论/评价流持续进入 → 分类（负面/竞品/广告/正常）→
高优先级恶评进入处理队列 → Agent 或规则决定（回复/退款/申诉/
忽略）→ 高风险动作人工审批 → 执行 → 复盘。

对架构的需求：

- **流式检测管线**：分类器可以是内部规则、LLM 工具或外部 ML
  服务。无论哪种，都需要"入站事件 → 检测工具 → 决策"的异步
  管线。这正好是 `IngressEvent` + `ToolRequest` 的组合。
- **人工审批流**：`ToolManifest.requires_approval` 目前只会返回
  `APPROVAL_REQUIRED` 拒绝。需要真正的 `APPROVAL_PENDING` 状态：
  op 挂起 → UI 展示 → 人类批准/拒绝/附言 → 继续执行或取消。
  退款、公开回复、申诉都是典型高风险动作。
- **置信度与证据**：恶评检测结果应携带置信度、原文引用、分类
  依据，进入审计与上下文。`ToolResultContract` 的
  declared/observed/possible effects 之外，还需要
  `evidence`/`confidence` 等结构化字段（可放进 data 契约）。

### 1.3 多平台接入管理

现实流程：商品、价格、库存、订单需要在多平台间同步；每个平台
有自己的 API、授权、限流、webhook 格式。

对架构的需求：

- **Integration 一等公民**：平台不是单个工具，而是一组能力：
  多个工具（拉单、回评、同步库存）+ 入站事件类型 + 凭证 +
  限流 + 健康状态。建议 `Integration` 注册对象 =
  {manifests, ingress_event_types, credentials_ref, rate_limits,
  endpoints, status}。内核只认这个统一契约。
- **凭证与密钥**：当前只有 LLM API key 从 env 读取；平台 token
  需要安全存储、轮换、按平台授权。需要 `CredentialStore`
  （引用式，密钥不入库/不入审计）。
- **幂等与限流**：平台 API 可能超时但实际已生效（下单、回评）。
  必须由执行器强制幂等键 + 超时后状态查询，而不是靠重试猜测；
  `EXTERNAL_IRREVERSIBLE` 工具需要补偿/对账策略。
- **跨平台一致**：库存扣减在多平台同时发生，需要内核提供
  "数字不变量"（如 stock >= 0）和预留/占用语义，而不只是
  路径级版本冲突。这引出 RecordStore（见 1.4）。

### 1.4 进销存记录

现实流程：采购入库、销售出库、盘点、调拨、供应商对账。需要
库存账本、订单记录、供应商记录，以及严格的审计与不变量。

对架构的需求：

- **结构化记录存储**：SharedKB 是"文档型"知识库（path → content），
  适合放话术、平台规则、商品手册；不适合放 SKU 库存、订单行、
  流水。需要 `RecordStore`：
  - 命名记录类型（sku / stock / purchase_order / sales_order /
    ticket / review）与 schema；
  - 记录级不变量（stock >= 0、金额不为负、单号唯一）；
  - 所有变更走 effect（`RECORD_UPSERT` / `RECORD_DELTA`），由
    CommitValidate 检查不变量，由统一 journal 支持回滚/对账。
- **账本语义**：库存流水应是 append-only 的 double-entry 风格
  （每次变动有凭证号、来源单据、数量、操作人、tick）。当前
  SharedKB 的"当前值 + 版本"不够，电商对账需要完整流水。
- **事务与回滚**：采购入库与库存增加、销售出库与库存减少必须
  同 tick 原子提交；这正是现有 TransactionBuffer 的用武之地，
  但需要把 RecordStore 纳入 effect 类型与回滚清单（与 FILE_WRITE、
  KB_WRITE 并列）。

---

## 2. 场景对现有架构判断的修正与强化

### 2.1 强化 OI-004 的判断：内核已够，但三个层次缺失

电商场景没有推翻 OI-004 的目标架构，而是把缺失层从两个扩展为
**四个**：

1. **ContextCompiler**：CS 坐席、库存管理员、店长看到的必须是
   完全不同的观察（ticket 上下文 / 库存看板 / 平台健康与 KPI）。
   当前全量同构观察完全无法表达。
2. **Runtime / Control Plane**：客服是实时业务，wall-clock、
   pause、slow-clock、人工介入必须真实可用。
3. **Ingress / Integration 层**：外部平台事件与工具必须有一等
   抽象。这是电商独有的、最大的新缺口。
4. **RecordStore / ApprovalGate**：结构化记录与人工审批是电商的
   刚需，也是另外两个场景（软件/小说）未来会需要的。

### 2.2 对"设计是否冗余"的补充

电商场景让一个事实更清楚：当前架构**在内核事务上偏重，在
边界抽象上偏轻**。

- 偏重的部分：10 阶段 tick、两阶段校验、多账本（Transaction/
  PendingOps/Outbox/Audit）对电商有价值，但必须在统一 journal
  上实现，否则每加一个 RecordStore 就多一份回滚清单。
- 偏轻的部分：没有 IngressBuffer、没有 RecordStore、没有真实
  Approval、没有 WorkerPool。这些不是"场景配置"能解决的，必须
  进内核或内核边缘。

### 2.3 对"灵活性是否充足"的补充

电商场景要求三种新的可扩展性，当前都不具备：

- **入站事件类型可扩展**：平台适配器必须能注册新 event_type，
  而不是改内核。
- **记录类型可扩展**：SKU/订单/ticket/review 应该可以由场景包
  定义 schema，而不是写死。
- **路由策略可扩展**：CS 池的负载均衡、恶评的优先级队列、
  进销存的按商品线路由，应该由策略配置。

---

## 3. 建议新增的抽象（可与 OI-004 目标架构合并）

```text
                     ┌──────────────────────────────┐
                     │  Control Plane (UI/API)      │
                     │  客服工作台 · 审批台 · 看板  │
                     └──────────────┬───────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │  SimulationRuntime           │
                     └──────────────┬───────────────┘
                                    │ run_tick
        ┌───────────────────────────▼──────────────────────────┐
        │  Kernel (10-phase, Unified TickJournal)              │
        │  Ingest(含 Ingress) → Freeze → Schedule(含 SLA)      │
        │  → Observe(ContextCompiler) → Decide → Validate      │
        │  → Act → Commit(含 RecordStore invariants)           │
        │  → Publish(含 Approval/Egress dispatch) → Audit      │
        └───────┬──────────────────┬──────────────────┬────────┘
                │                  │                  │
   ┌────────────▼──────┐  ┌────────▼────────┐  ┌──────▼─────────┐
   │ ContextCompiler   │  │ RecordStore     │  │ Ingress/Egress │
   │ 角色/ticket/KB    │  │ sku/order/ticket│  │ 平台适配器     │
   │ 术语/话术注入     │  │ ledger+invariant│  │ 去重/限流/凭证 │
   └───────────────────┘  └─────────────────┘  └────────────────┘
```

新增抽象说明：

1. **IngressBuffer / IngressEvent**
   - `IngressEvent`：source（platform）、external_id、event_type、
     occurred_at_wallclock、payload、idempotency_key、priority、
     deadline_hint。
   - Ingest 阶段消费，`(source, external_id)` 去重；可转成
     WakeEvent / TaskCreate / TicketRecord 或直接投递给 Agent。
   - 事件持久化后再向平台 ack（防丢）。

2. **RecordStore**
   - 类型化记录 + schema 校验；`RECORD_UPSERT` / `RECORD_DELTA`
     effect；不变量检查（库存非负、单号唯一、金额合法）。
   - append-only ledger 投影出当前库存；对账、审计、重放都从
     ledger 推导。
   - 与 SharedKB 分工：文档/话术/规则 → KB；库存/订单/ticket →
     RecordStore。

3. **ApprovalGate**
   - `requires_approval` 不再直接拒绝，而是生成
     `HUMAN_APPROVAL` pending op；UI 批准/拒绝/附言；批准后由
     dispatch 继续执行。
   - 审批有 deadline、升级策略、审计记录（谁在什么上下文批的）。

4. **WorkerPool / 路由**
   - `DelegateIntent` 的 `recipient` 从"特定 Agent"扩展为
     "Agent 或 WorkerPool"；池按技能/负载/优先级路由到具体坐席。
   - 任务树和邮件照旧，只是多一个路由层。

5. **Integration 注册对象**
   - 平台适配器 = manifests + ingress_event_types + credential_ref +
     rate_limits + endpoints + health。
   - `ExecutorRegistry` 之外增加 `IntegrationRegistry`；或把
     Executor 扩展为 Integration 的特例（只出站，无入站）。

6. **SlaPolicy 与优先级调度**
   - 任务/ticket 携带 `deadline_tick + priority`；Schedule 阶段
     按优先级+deadline 排序就绪集。
   - 到期前 N tick 生成 `DEADLINE_APPROACHING` 事件（现在没有
     生成者），超时走结构化升级（通知 Manager/人工）。

---

## 4. 距离"真正可用的电商模拟"还差什么

按依赖顺序：

1. **基础修复**：OI-003 的 P0-1/P0-2/P0-3 必须先修（电商有大量
   外部副作用与回滚，路径穿越和账本漏账不可接受）。
2. **Runtime + Journal**：wall-clock 循环、tick duration 生效、
   统一 journal（库存/订单/事件/审批都从 journal 投影）。
3. **Ingress + 适配器框架**：一个平台模拟器（可先做假平台）能
   推消息、收回复、触发 webhook；演示多平台消息去重与 SLA。
4. **RecordStore + 库存**：SKU/库存/订单/采购单四种记录类型，
   采购入库与销售出库两个原子流程。
5. **ApprovalGate + Human Worker**：高风险回复/退款走人工审批；
   用户作为客服主管 Worker 在组织树中可被委派。
6. **ContextCompiler 场景化**：客服坐席 prompt 注入 ticket 详情、
   订单、商品、话术 KB；店长注入平台 KPI、未处理恶评数、库存
   预警。
7. **成本/限流**：按平台、按 Agent 的 token/cost/rate-limit
   预算与 Admission（P2-11 自然落地）。

---

## 5. 路线图更新（无向后兼容）

- **v0.9 — 真实运行内核**：P0 修复 + SimulationRuntime +
  Control Plane + LLM dispatcher + 统一 journal。
- **v0.10 — 边界能力**：IngressBuffer + 平台适配器框架 +
  RecordStore + ApprovalGate + 工具插件 API + KB 读取/注入。
- **v0.11 — 三场景包**：
  - 软件开发公司模拟
  - 小说写作工作室模拟
  - 电商平台管理模拟（最后做，但作为通用性验收）
- **v1.0 — 可用性**：成本/预算、审计回放、确定性重放；三个
  场景各有一个端到端 demo。

---

## 验收标准（本 OPEN_ISSUE 关闭条件）

- [ ] 有 IngressEvent/IngressBuffer 设计（含去重、排序、ack、
      持久化、平台事件类型注册）
- [ ] 有 RecordStore 设计（类型化记录、不变量、ledger 投影、
      effect 与回滚）
- [ ] 有 ApprovalGate 设计（HUMAN_APPROVAL op、UI、deadline、
      审计）
- [ ] 有 WorkerPool/路由设计（委派目标从 Agent 扩展到池）
- [ ] 有 Integration 注册对象设计（manifest + 入站事件 + 凭证 +
      限流 + 健康）
- [ ] 电商场景包配置可在不改内核代码的前提下定义
- [ ] 与 OI-004 的目标架构图合并为一份总架构文档
