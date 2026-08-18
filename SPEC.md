# My-Team 多智能体协作框架设计 Spec

**版本:** v0.9.0 目标架构
**定位:** AI 辅助的一人公司（One-Person Company）运行框架
**前身:** v0.1.0–v0.8.0 实现细节保留在 `SPEC.v0.8.legacy.md`

---

## 0. 定位与目标

本系统用于运行一个**由单人所有、多智能体协作、面向真实业务场景的
"一人公司"**。人类是公司的所有者与最终决策人；AI Agent 是员工，
按组织树分工、异步协作、在事务化时间步中推进工作。

### 0.1 五个目标场景

1. **软件开发公司模拟**：需求 → 设计 → 任务拆分 → 实现 → 评审 →
   测试 → 交付。
2. **小说写作工作室模拟**：大纲 → 分章 → 创作 → 审校 → 修订 →
   成稿。
3. **电商平台管理**：智能客服、恶评检测、多平台接入、进销存。
4. **社交平台自媒体**：选题 → 创作 → 审核 → 多平台发布 → 互动 →
   数据复盘。
5. **知识星球运作**：内容日历、会员服务、社区审核、知识库运营、
   商业变现。

### 0.2 设计目标

- **分层同抽象**：每个 Agent 只看到其层级应当看到的上下文，
  在有限上下文窗口内做最优决策。
- **实时运行**：系统按 wall-clock 推进；人类可以调慢时钟、暂停、
  单步，并在任意 tick 通过消息或审批注入意见。
- **灵活扩展**：新场景 = 场景包（组织、工具、记录模型、外部适配器、
  日历、审批策略、知识库种子、KPI 视图），不修改内核。
- **事务可靠**：每个 tick 的副作用原子提交；失败可回滚；外部操作
  幂等、可重试、可审计。
- **服务对象**：小规模个体户与一人公司。用户无需软件开发背景，
  通过安装 Skill 包与场景包即可获得一支可审计、可暂停、可插手的
  AI 团队；开发者可通过 MCP 与 ToolPlugin 接入外部能力。

### 0.3 非目标

- 不是通用 Agent 框架（LangChain/AutoGen 的替代品）。
- 不做通用 Bash 沙箱（在沙箱协议完成前，见 OI-001）。
- 不做多实例并发写同一 DB（SQLite 单写者；v1.0 前不引入分布式）。
- 不追求无限规模（单个一人公司规模：个位数到几十个 Agent）。

---

## 1. 核心设计原则

1. **Tick 是提交单位，ReAct 是行为协议**：内核按离散 tick 推进，
   每 tick 状态提交一次；Agent 的思考-行动循环（ReAct）可跨多个
   tick。
2. **同一抽象水平思考**：观察必须按角色/任务裁剪。Root 看目标、
   任务树、风险与 KPI；Manager 看工作流与子级状态；Worker 看当前
   任务、相关文件、相关知识与最近消息。
3. **异步外部交互**：LLM、工具、人类决策、外部平台全部通过
   pending operation / ingress event 异步进行；任何 tick 阶段不得
   同步等待外部调用。
4. **默认拒绝，显式授权**：工具、平台、审批、知识库读取全部
   deny-by-default。
5. **人类是一等参与者**：人类可以是 Owner、Worker、Approver；
   人类任务与审批走与 AI 相同的事务路径。
6. **单一事实源**：所有状态变更写入统一 TickJournal；审计、回放、
   对账、恢复都是 Journal 的投影。
7. **场景包 = 配置 + 插件**：业务场景不修改内核。

---

## 2. 总体架构

```text
┌────────────────────────────────────────────────────────┐
│                 Control Plane (HTTP/WS/UI)              │
│  启停/调速/单步 · 消息 · 审批台 · 工作台 · 看板 · 审计   │
└────────────────────────┬───────────────────────────────┘
                         │ 命令 / 查询
┌────────────────────────▼───────────────────────────────┐
│              SimulationRuntime (wall-clock 循环)         │
│  tick duration 生效 · pause/resume/step · 事件总线       │
└────────────────────────┬───────────────────────────────┘
                         │ run_tick
┌────────────────────────▼───────────────────────────────┐
│                 Kernel（10 阶段，唯一阶段机）             │
│  Ingest → Freeze → Schedule → Observe → Decide →        │
│  Validate → Act → Commit → Publish → Audit              │
│  统一 TickJournal                                        │
└──────┬───────────────┬───────────────┬──────────────────┘
       │               │               │
┌──────▼───────┐ ┌─────▼────────┐ ┌────▼──────────────────┐
│ContextCompiler│ │ Capability   │ │ Ingress/Egress +      │
│ 角色化观察    │ │ 工具/执行器  │ │ Integration 适配器     │
│ KB/KPI 注入  │ │ 策略/审批    │ │ 外部平台事件与请求      │
└──────────────┘ └──────────────┘ └────────────────────────┘
       │               │               │
┌──────▼───────────────▼───────────────▼──────────────────┐
│ 数据层：PrivateStore · SharedKB · RecordStore ·          │
│          AssetStore · CredentialStore · MailStore        │
└──────────────────────────────────────────────────────────┘
```

### 2.1 组件职责

| 组件 | 职责 |
|---|---|
| Control Plane | 对外 API/UI；人类操作入口 |
| SimulationRuntime | wall-clock 循环；tick 调度；duration 变更 |
| Kernel | 10 阶段 tick；状态提交/回滚；Journal 写入 |
| ContextCompiler | 按 ObservationPolicy 为每个 Agent 编译 briefing |
| Capability Layer | 工具注册/策略/执行器分级/审批 |
| Ingress/Egress | 外部平台事件入站、请求出站、适配器管理 |
| 数据层 | 文件、知识、记录、资产、凭证、邮件的存储与查询 |

---

## 3. 内核：Tick、阶段与 Journal

### 3.1 十阶段定义

每 tick 顺序执行（与 v0.8 实现语义一致，但把 Deliver 并入 Ingest
阶段，避免阶段模型分裂）：

| # | 阶段 | 输入 | 输出 | 规则 |
|---|---|---|---|---|
| 1 | Ingest | 外部结果、Ingress 事件、到期定时器 | 完成/超时/失败结果投递；Ingress 事件入队；邮件投递 | fence 过期 epoch；去重 |
| 2 | Freeze | 当前全局状态 | 冻结快照（含私有文件视图、KB 视图、Record 视图、KPI 投影） | 本 tick 内所有 Agent 见同一快照 |
| 3 | Schedule | 快照 + 事件 + 日历规则 | 就绪 Agent 集合（按优先级/deadline 排序） | 每 Agent 每 tick 最多 1 次 activation |
| 4 | Observe | 快照 + 就绪集 | 角色化 AgentObservation（由 ContextCompiler 编译） | 只读，不产生副作用 |
| 5 | Decide | AgentObservation + Continuation | Intent 列表 | 非阻塞；不允许同步 LLM/工具/人类 |
| 6 | Validate | Intent 列表 | 通过/失败结果（带错误码） | PreValidate：允许尝试吗？ |
| 7 | Act | 通过验证的 Intent | staged effects + pending op 注册 + 审计草稿 | 只登记，绝不应用 |
| 8 | Commit | staged effects + 本 tick 注册的 op | 提交或整 tick 回滚 | CommitValidate：现在仍可提交吗？ |
| 9 | Publish | 已提交效果 + SUBMITTED ops | dispatch（LLM/工具/审批/出站）；下一 tick 可见事件；日历触发 | 提交成功才 dispatch |
| 10 | Audit | Journal | 审计事件（是 Journal 的视图） | 全量记录 |

**关键规则**：
- Act 注册的 pending op 属于本 tick 事务；Commit 回滚时一并撤销，
  且恢复 Agent 状态与 Continuation（修复 OI-003 P0-2）。
- Publish 只在 Commit 成功后执行；回滚 tick 不产生任何 dispatch。
- Ingest 是唯一允许外部结果进入内核的入口。

### 3.2 统一 TickJournal

- 每个 tick 产生一个 `TickRecord`（append-only），包含：
  - 快照哈希、epoch、tick；
  - 所有 Intents 与验证结果；
  - 所有 staged effects 与最终状态（committed/failed/rolled_back）；
  - 本 tick 的 pending op 注册与取消；
  - outbox 条目；
  - 审批请求与结果；
  - 审计事件。
- Commit 成功后 Journal 提交；rollback 时 Journal 标记 aborted。
- PendingOperationRegistry、Outbox、AuditLog、RecordStore、KPI
  都是 Journal 的投影（projection）。
- 持久化：Journal 落 SQLite（或后续的追加文件），保存/恢复从
  Journal 重放。

### 3.3 事务与回滚

- 回滚对象：文件写入/补丁、KB 写入、Record 变更、任务变更、邮件
  outbox 条目、pending op 注册、审批请求、Agent 状态/Continuation。
- 回滚后 state_epoch 递增；所有基于旧 epoch 的外部结果 fence 为
  stale。
- 外部副作用不可回滚：LLM 成本、平台 API 已生效写入。此类操作
  必须声明 `reversible=false` 并走补偿/对账路径。

---

## 4. 核心实体

### 4.1 Agent 与组织树

```python
AgentConfig:
  agent_id: str
  display_name: str
  kind: "llm" | "human" | "service"     # 新增
  role: str                              # 角色（场景包定义）
  parent_id: str | None
  children: list[str]
  worker_pools: list[str]                # 新增：所属 WorkerPool
  tools: list[str]                       # 能力授权（工具名）
  observation_policy: str                # 新增：观察策略名
  llm_profile: str | None                # kind=llm
  human_queue: str | None                # kind=human
  service_ref: str | None                # kind=service
  metadata: dict
```

- 组织树静态；日常运行不变更父子关系。
- `kind=human` 的 Agent 由 UI 队列驱动，不由 LLM 驱动。
- `kind=service` 的 Agent 是外部服务的代理（可选优化）。

### 4.2 任务 Task

在 v0.8 Task 基础上增加：

- `sla_ticks: int | None`：响应/完成时限；
- `priority`：调度排序；
- `depends_on: list[task_id]`：任务依赖（新增，B 阻塞于 A）；
- `owner_pool: str | None`：当委派到 WorkerPool 时由路由层选择
  具体 Agent（新增）；
- `source_event_id`：来源 Ingress 事件（客服 ticket 等）；
- `artifacts`：文本引用或 Asset 引用。

### 4.3 消息 / Email

Email 是 Agent 间异步协作的正式渠道，并扩展为通用消息模型：

- `from`、`to`、`cc`、`thread_id`、`subject`、`body`；
- `attachments: list[AttachmentRef]`（新增）：
  `{ref_type: shared_kb|asset|private_transfer, path, version, hash,
   size, mime}`；
- `email_type`（沿用 v0.8 的 13 类 + `HUMAN_APPROVAL_REQUEST`）；
- 大内容只存引用，不复制正文。

### 4.4 外部联系人 Contact（新增）

- 服务对象不是 Agent：客户、会员、粉丝、供应商。
- `contact_id`、`platform`、`external_id`、`display_name`、
  `tags`、`segments`、`metadata`。
- 与 Ticket/Post/Order 等 Record 关联。

### 4.5 记录 Record（新增）

- `RecordStore` 中的类型化记录：`sku`、`stock_movement`、
  `purchase_order`、`sales_order`、`ticket`、`review`、`member`、
  `subscription`、`content_asset`、`metric_snapshot` 等。
- 记录类型由场景包 schema 定义；内核提供 `RECORD_UPSERT`、
  `RECORD_DELTA` 两类 effect 与不变量检查。

### 4.6 资产 Asset（新增）

- 二进制或大文本对象；内容寻址（sha256）；MIME、size、metadata。
- 私有文件与 Email 附件基于 AssetStore 引用。

---

## 5. 上下文模型（ContextCompiler）

### 5.1 ObservationPolicy

每个角色声明一个观察策略：

```json
{
  "role": "root",
  "sections": ["mission", "task_tree_summary", "kpi_dashboard",
               "escalations", "pending_decisions"],
  "task_scope": "all",
  "kb_injection": {"enabled": true, "max_entries": 5,
                   "sources": ["glossary", "decision_log"]},
  "max_tokens": 3000
}
```

- `task_scope`：`all | subtree | owned | focus_task`。
- `kb_injection`：按当前任务与消息正文的关键词注入术语/规则。
- `kpi_dashboard`：从 Journal/RecordStore 投影计算的角色化指标。

### 5.2 Briefing 结构

ContextCompiler 为每个激活的 Agent 编译 briefing：

```text
[身份与目标] 我是谁、公司目标、本 tick 时间
[专注任务]   当前 continuation 绑定的 task：描述、验收标准、
             deadline、依赖、我的进度
[收件箱]     最近邮件全文（含附件清单）或摘要（超预算时）
[相关知识]   自动注入的 KB 条目（术语/规则/决策）
[数据看板]   本角色关心的指标（root/manager）
[记忆]       最近 N 轮 ReAct 摘要与未完成承诺
[可用工具]   由 ToolManifest 自动生成的工具定义
```

- 所有内容受 token budget 约束；超出时按优先级截断并标记
  `[truncated]`。
- 邮件正文默认进入上下文；超大时摘要 + 引用。

### 5.3 专注点漂移控制

- 每个 Agent 的 `AgentContinuation.task_id` 为当前专注任务；
- 观察默认围绕该任务；Agent 只有通过显式工具（如 `task_tree_view`）
  才能 zoom out；
- 切换任务需完成或转交当前任务，避免上下文漂移。

---

## 6. 能力模型（工具与集成）

### 6.1 ToolManifest 与 OperationPolicy

沿用 v0.8 并强化：

- `ToolManifest` 增加 `approval_policy`（何时需要人工审批）、
  `ingress_event_types`（工具是否消费入站事件）、`egress`（是否
  外部出站）、`compensation_tool`（不可逆操作的补偿工具）。
- `OperationPolicy` 继续 deny-by-default；策略必须覆盖：
  allowlist、审批、网络、文件作用域、墙钟/输出上限、不可逆、
  速率上限。

### 6.2 ToolPlugin API（公共扩展）

```python
sim.register_tool(
    manifest=ToolManifest(...),
    handler=my_handler,          # (context, **args) -> ToolResult
    executor=None,               # 可选：外部执行器
    policy=OperationPolicy(...), # 可选：默认拒绝
)
```

- handler 只通过 `ToolContext` 与内核提供的 subsystem handles
  访问数据层，不接触 Simulation 私有成员。
- LLM 工具定义从 `ToolManifest.input_schema/output_schema`
  自动生成；删除手写工具表。
- 场景包的工具在加载时注册并校验。

### 6.3 执行器分级与 Admission

沿用 v0.8：TRUSTED_IN_PROCESS / UNTRUSTED_OUT_OF_PROCESS /
SANDBOXED_OUT_OF_PROCESS；并增加：

- 出站工具（EXTERNAL_IRREVERSIBLE）必须提供幂等键与状态回查；
- 平台级 Admission：按 Integration 的 rate_limit 与健康状态背压。

### 6.4 Integration（外部平台适配器）

```python
Integration:
  name: str
  credential_ref: str
  rate_limits: dict
  manifests: list[ToolManifest]      # 出站能力
  ingress_event_types: list[str]     # 入站事件类型
  webhook_endpoint: str | None
  health_check: str
```

- 平台适配器 = Integration；内核只认 Integration 契约。
- 凭证通过 CredentialStore 引用，不进 Journal/审计。

### 6.5 MCP Provider Adapter

MCP（Model Context Protocol）与 Skill 是不同层次的能力供给：

- **MCP** 解决"工具从哪来"：把外部 MCP server 暴露的 Tool 资源
  接入内核，面向开发者/集成者。
- **Skill** 解决"一件事怎么做"：把 SOP、提示词、工具集、知识与
  审批策略打包，面向非专业用户（见 §11.4）。

MCP 接入规则：

```text
MCP server（stdio / HTTP / SSE）
  → MCP Adapter 枚举 tools/resources
  → 自动生成 ToolManifest（name/version/input_schema/output_schema
     从 MCP tool schema 映射）
  → 执行器注册为 UNTRUSTED_OUT_OF_PROCESS（本地 stdio/子进程）
     或 EXTERNAL_IRREVERSIBLE（远程 HTTP）
  → 调用经 ToolRequest/ToolResultContract 与 pending op 路径
```

- identity 字段（agent_id/task_id/state_epoch/manifest_hash）仍由
  内核注入，MCP server 不得自指。
- MCP 工具默认 deny-by-default：必须显式加入 OperationPolicy
  allowlist 后才能被 Agent 使用。
- 适配器负责限流、超时、重试与结果契约转换；MCP 的
  `resources` 可映射为 SharedKB 或 AssetStore 的只读引用。

---

## 7. 数据与存储

### 7.1 PrivateStore（私有工作空间）

- 路径解析统一走 `PrivateStore.resolve_path`；任何写路径必须先
  通过 resolve 与访问控制（修复 OI-003 P0-1）。
- 文件读经冻结视图；文件写经 effect。

### 7.2 SharedKB（文档型知识库）

- 增加 `kb_read`、`kb_list`、`kb_search` 工具（v0.8 只有 kb_write）。
- 条目类型：`document`、`glossary`、`style_guide`、`decision_log`、
  `faq`、`response_template`。
- 读取与注入必须经 PermissionEngine。
- `kb_search` 先做关键词/路径匹配，后续演进到 embedding。

### 7.3 RecordStore（结构化记录）

- 记录类型由场景包 schema 定义；注册即校验。
- 所有变更走 effect；CommitValidate 检查记录级不变量
  （库存非负、单号唯一、金额合法、到期日合法）。
- append-only ledger 投影当前状态；审计/对账/重放从 ledger 推导。
- 典型记录族：
  - 电商：`sku`、`stock_movement`、`purchase_order`、
    `sales_order`、`ticket`、`review`；
  - 自媒体：`content_asset`、`publish_job`、`metric_snapshot`；
  - 知识星球：`member`、`subscription`、`post`、`comment`。

### 7.4 AssetStore（二进制资产）

- 内容寻址（sha256）；`put/get/stat`；
- 私有文件快照支持二进制读取（v0.8 直接跳过二进制）；
- Email 附件引用 AssetStore 对象或 SharedKB 条目。

### 7.5 CredentialStore（凭证）

- 引用式：`credential_ref` → 外部 KMS/env/加密文件解析；
- 密钥不落 DB、不进 Journal、不进审计、不进 prompt。

---

## 8. 消息、入站与出站

### 8.1 IngressBuffer / IngressEvent

```python
IngressEvent:
  source: str              # "douyin" / "taobao" / "github" / ...
  external_id: str         # 平台侧唯一 ID
  event_type: str
  occurred_at: str         # wall-clock 时间
  payload: dict
  idempotency_key: str
  priority: str
  deadline_hint: str | None
```

- Ingest 阶段消费；`(source, external_id)` 去重（持久化，跨重启）。
- 事件持久化成功后才向平台 ack（防丢）。
- **映射前门**：事件进入内核后统一走 `IngressEvent → ProcessInstance`
  （实例化流程），不再直接转 WakeEvent / TaskCreate / Record / Email；
  该映射与流程实例化属 v0.11 编排层（E1 process-model，最小测试向量
  首段）。v0.10 只交付方向中立的传输层：IngressBuffer / 去重 / ack /
  Integration 注册 / 出站 pending op；Ingest 阶段可唤醒相关 Agent
  （"有事件到达"），但不隐式决定下游对象（任务/记录/邮件由流程显式生成）。

### 8.2 EgressRequest

- 出站请求统一为 pending op（`EXTERNAL_IRREVERSIBLE` 或
  `EXTERNAL_SAFE` 工具），带幂等键、状态回查、补偿工具。
- 出站执行器可运行在独立 worker；结果 ingest 与工具结果同路径。

### 8.3 HumanMessage

- 人类消息是 Ingress 的特例：`source="human"`。
- 人类可向任意 Agent 发消息；Agent 通过正常收件箱看到。

---

## 9. 调度：日历、SLA 与 WorkerPool

### 9.1 Calendar Scheduler

- `ScheduleRule`：`{rule_id, target, cron | interval_ticks,
  next_run_tick, action}`；
- 每 tick 评估，到期生成 TIMER_EXPIRY 事件或创建任务；
- 支持"每日发布""每周选题会""到期前 N tick 提醒"。

### 9.2 SLA 与优先级

- Task 携带 `deadline_tick` 与 `priority`；
- Schedule 阶段按 `(priority, deadline_tick)` 排序就绪集；
- 到期前 `N` tick 生成 `DEADLINE_APPROACHING` 事件；
- 超时走结构化 escalation（`on` = unresolved | condition_breached |
  exception，`mode` = arbitrate | transfer | advise），不硬编码
  「通知 Manager → 转人工 → 关闭」阶梯。

### 9.3 WorkerPool

- `WorkerPool`：一组同质 Worker + 路由策略（round_robin /
  least_busy / skill_match）。
- `DelegateIntent.recipient` 可以是 `agent_id` 或 `pool_id`；
  池路由发生在 Act/Commit 阶段，选择结果写入 Journal。

---

## 10. 人类参与与审批

### 10.1 Human Worker Agent

- `kind=human` 的 Agent 有任务队列；Manager 像对 AI Worker 一样
  委派任务；
- 人类通过 UI accept/complete/fail；动作翻译为 Intent，走相同
  事务路径；
- 人类任务有 deadline 与升级策略（超时提醒 Manager）。

### 10.2 ApprovalGate（统一为 HumanTask）

- 审批不再用独立的 `HUMAN_APPROVAL` pending op 建模，而是统一为
  **HumanTask**（`kind = work | approval | decision | consultation`，
  见 E1 process-model），吸收 ApprovalGate / Human Worker / HumanMessage
  的重叠语义。
- 审批触发 = 编排层 gate（引用 `authority_ref`）+ **三查分离**：
  1. Capability：调用者能否调用（OperationPolicy，属闭包
     deny-by-default）；
  2. Authority：调用者是否有权作出该决策（Authority 裁决）；
  3. Gate：流程是否完成必要审批。
  三者互不替代——`content.final` 不豁免 OperationPolicy 的 approval。
- 人类参与身份须经认证（Identity 闭包，见 §12.1：`from/to` 身份字段
  由内核注入，Agent 不可自指、不可伪造）。
- 审批有 deadline；未决/超时走结构化 escalation（`on`/`mode`/`target`）。
- 审计记录谁在什么上下文批的。
- **版本切分**：v0.10 交付 Human Worker（kind=human Agent，§10.1）与
  CredentialStore（§7.5）；ApprovalGate 的 HumanTask 模型与三查分离
  属 v0.11（E1/E2）。

---

## 11. 场景包

### 11.1 场景包结构

```text
scenario/
├── scenario.json           # 场景名、版本、描述
├── org_tree.json           # 组织树（可含 human worker、pools）
├── roles.json              # role → observation_policy/工具集
├── tools/                  # manifest + handler/executor 引用
├── record_schemas/         # RecordStore 记录类型定义
├── ingress_adapters/       # 平台适配器（webhook/轮询）
├── schedules.json          # 日历规则
├── approval_policies.json  # 审批策略
├── kb_seed/                # 术语表、规则、话术、决策日志
└── kpi_dashboards/         # root/manager 指标视图
```

### 11.2 加载与校验

- 场景包加载即校验：工具 manifest 合法、记录 schema 合法、
  审批策略合法、组织树无环、pools 存在；
- 任何非法项拒绝加载并给出结构化错误。

### 11.3 五个场景包

| 场景 | 关键工具 | 关键 Record | 入站事件 | 人类角色 |
|---|---|---|---|---|
| 软件公司 | apply_patch/run_tests/git_diff/code_search | issue/pull_request | GitHub | 客户/工程经理 |
| 小说工作室 | apply_patch/kb_read/doc_diff | chapter/review | 无 | 作者/主编 |
| 电商 | kb_search/inventory_*/order_*/reply | sku/order/ticket/review | 平台消息/评价/订单 | 客服主管/审批人 |
| 自媒体 | content_plan/asset_*/publish_*/metric_* | content_asset/publish_job | 评论/数据回传 | 老板/终审 |
| 知识星球 | kb_search/content_calendar/member_*/post_* | member/subscription/post | 帖子/评论/会员事件 | 星主 |

### 11.4 Skill Package（面向非专业用户的能力封装）

Skill 是一组"提示词 + SOP + 工具集 + 知识 + 审批策略"的可装卸单元，
回答"Agent 如何做好一类具体工作"。面向不会写代码的个体户：

```text
skill/
├── SKILL.md            # 人读说明书：做什么、何时触发、SOP、边界
├── prompts/            # 角色/任务提示词模板
├── tools/              # 可选：本技能需要的受限工具（manifest+handler）
├── kb_seed/            # 可选：术语表、话术、规则、模板
├── scripts/            # 可选：运行于 L0/L1 的脚本
└── approval.json       # 哪些动作必须人工审批
```

- Skill 安装/卸载不修改内核；安装即校验。
- ContextCompiler 按触发条件注入：任务标题、邮件正文或 KB 关键词
  命中 Skill 的触发条件时，将该 Skill 的 SOP、模板与相关知识注入
  对应 Worker 的 briefing。
- Skill 与 Tool 的分工：Tool 是一个可执行能力；Skill 是"用这些
  能力做好一类工作"的业务封装。
- Skill 与场景包的关系：场景包 = 组织 + 多个 Skill + 记录模型 +
  外部适配器 + 日历 + KPI。

---

## 12. 安全与不变量

1. **身份**：ToolContext 只由内核创建；from/to 身份字段系统注入，
   Agent 不可自指。
2. **路径**：一切写路径经 PrivateStore.resolve_path；拒绝 `..`、
   绝对路径越界与 symlink 逃逸。
3. **权限**：工具、KB、记录、资产的访问全部显式授权。
4. **凭证**：引用式存储，不进 Journal/审计/prompt。
5. **事务**：一个 tick 要么完整提交，要么完整回滚；不存在孤儿
   pending op（OI-003 P0-2）。
6. **幂等**：Ingress 按 `(source, external_id)` 去重；出站按
   idempotency key 去重；ToolRequest 按 request_id 去重。
7. **可审计**：审计是 Journal 的视图；任何人可追溯任一 tick 的
   状态与决策。
8. **Epoch fencing**：外部结果携带 state_epoch；旧 epoch 结果
   一律 fence。
9. **默认拒绝**：未注册工具、未授权集成、未审批高风险操作一律
   拒绝。
10. **非目标边界**：LOCAL_PROCESS 只防意外不防恶意；安全边界从
    SANDBOXED_PROCESS 开始。

---

## 13. 控制平面 API 草案

```text
POST /runtime/start           启动 wall-clock 循环
POST /runtime/pause           暂停（下一 commit 边界生效）
POST /runtime/resume          恢复
POST /runtime/step            单步（执行一个 tick 后暂停）
PUT  /runtime/tick-duration   {value, unit, effective_tick}
GET  /runtime/status          tick、state、epoch、agent 数、pending ops

GET  /org/tree                组织树
GET  /tasks                   任务树与 SLA 状态
GET  /tickets                 客服/会员/内容工单

POST /messages                人类发消息给 Agent/WorkerPool
GET  /agents/{id}/inbox       收件箱
POST /approvals/{id}/decision 审批：approve/reject + 附言
GET  /approvals/pending       待审批队列

GET  /kb/{path}               读取有权限的 KB 条目
POST /kb/search               {query, scope, max_entries}

GET  /records/{type}          查询记录
GET  /kpi/{dashboard}         指标看板
GET  /audit?tick=...          审计查询
```

---

## 14. 关键不变量（验收级）

- [ ] 一个 tick 只产生一个 TickRecord；回滚 tick 不产生 dispatch。
- [ ] 任何 pending op 都有明确的创建 tick 与终止 tick；不存在
      跨 tick 孤儿 op。
- [ ] 文件写路径不可能逃逸 Agent 私有空间。
- [ ] 回滚后 Agent 状态/Continuation 与 tick 开始前一致。
- [ ] `run_tick` 返回的 phases/committed/errors 与 Journal 一致。
- [ ] 每个场景包加载后，内核代码不变。
- [ ] 外部事件 `(source, external_id)` 跨重启去重。
- [ ] 审批通过前，高风险工具不得执行。

---

## 15. 版本路线图

- **v0.9 — 内核可信与真实运行**：P0 修复、统一 Journal、
  Runtime/Control Plane、LLM dispatcher、ContextCompiler 最小版。
- **v0.10 — 边界能力**：Ingress/Egress、RecordStore、AssetStore、
  ToolPlugin API、KB 读取与附件、Calendar/WorkerPool、
  ApprovalGate、Human Worker。
- **v0.11 — 五场景包与扩展协议**：软件公司、小说工作室、电商、
  自媒体、知识星球各一个可运行 demo；MCP Provider Adapter；
  Skill Package 系统与首批技能包。
- **v1.0 — 一人公司可用**：成本/预算、审计回放、确定性重放、
  五场景端到端验收；README 面向个体户的安装/托管路径。

---

## 16. 与旧版 SPEC 的关系

本文件是 v0.9 目标架构 Spec，取代 SPEC.v0.8.legacy.md 作为当前
设计权威。旧版保留为 v0.1–v0.8 实现的历史记录。迁移过程中，
v0.8 已实现的机制（ToolManifest、OperationPolicy、PendingOps、
Outbox、SQLite 持久化、L0/L1 Python 执行）继续有效，但按本 Spec
逐步重构到统一 Journal 与插件化边界层。
