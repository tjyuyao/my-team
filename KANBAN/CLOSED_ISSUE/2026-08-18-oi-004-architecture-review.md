---
kind: issue
status: closed
source: 设计初衷（分层同抽象/实时参与/Email 注入）· 未来想法（附件、精细 KB、用户即 Worker、外部服务）· 应用场景（软件开发公司模拟、小说写作工作室模拟）
priority: high
---

# OI-004: 架构补充审查 — 分层抽象、实时运行与场景化能力

**Opened:** 2026-08-17（针对设计初衷与两个应用场景的补充架构审查）
**Status:** CONVERTED — 已拆分为 TODO（v0.9/v0.10 基础）
**Converted to TODO:** 2026-08-17-unified-tick-journal.md, 2026-08-17-runtime-control-plane.md, 2026-08-17-context-compiler.md, 2026-08-17-tool-plugin-and-manifest-tools.md, 2026-08-17-kb-read-and-attachments.md

---

## 0. 结论摘要

当前内核把"事务化的 tick 调度"做得相当认真，但离设计初衷还差三个
关键层次，它们都不在内核里：

1. **上下文编译器（Context Compiler）**——"同一抽象水平思考"目前
   没有实现。所有 Agent 看到的是同构的全局观察：全部任务列表、全部
   KB 路径、邮件主题。Root 和 Worker 的区别只在工具集合不同。
2. **运行时（Runtime / Control Plane）**——"实时运行、调慢时钟、
   用户中途注入意见"目前没有实现。`run()` 是同步紧循环，tick
   duration 不生效，LLM 请求没有调度器，人工审批没有落地。
3. **能力层（Capability / Tool Plugin）**——工具、外部服务、KB 读取、
   附件全部内联在 `Simulation` 里，用户无法为"软件开发公司"或
   "小说工作室"添加场景工具而不改内核。

此外，当前设计存在**两类冗余**：一类是"僵尸组件"（TickEngine、
Executors、IdentityEnforcer、FileOps、DelegationProtocol 等未接入
主路径）；另一类是"重叠的账本"（TransactionBuffer、PendingOps、
Outbox、Audit 各自记录，回滚时容易漏账，P0-2 已证明）。

建议的架构动作可以激进：**保留 tick 事务内核，但在其上下各增加
一层；同时用统一日志（unified journal）取代多账本。**

---

## 1. 设计初衷 vs 现状：逐条差距

### 1.1 "各层级 Agent 在同一抽象水平思考"

现状：`_phase_observe` 构造的 `AgentSnapshot` 中
`task_states` 是**全量任务表**，`shared_kb_snapshot` 是**全量 KB
路径与版本**，没有按角色、按任务归属、按权限裁剪。`PromptTemplates`
把全部任务都渲染成 "Your tasks:"。

后果：
- Root 没有看到"全局目标 / 任务树 / 风险 / 资源"的宏观视图；
- Worker 没有看到"我当前任务 + 相关文件 + 相关 KB 词条"的微观视图；
- 所有 Agent 被同一种模板驱动，专注点漂移是必然的。

需要的设计：
- 引入 **ObservationPolicy**（按 role/agent_id 声明的观察形状），
  或更完整地引入 **ContextCompiler**：为每个 Agent 编译一份
  token-budgeted briefing：
  - Root：目标与不变量、任务树摘要、下级状态、升级的阻塞/风险、
    pending decisions、成本/预算。
  - Manager：本工作流任务、子 Agent 状态、待处理邮件全文、
    相关 KB 条目。
  - Worker：当前任务（描述+验收标准）、最近邮件全文、
    工作区文件清单、任务相关 KB 定义。
- 每个 Agent 的 `AgentContinuation` 应绑定一个 `task_id` 作为
  **当前专注**，观察默认围绕该 task 展开，必要时通过工具请求
  更多上下文（zoom in/out）。

### 1.2 "有限上下文下表现最优"

现状：`LLMAgent` 只发送一条 system 消息；邮件只渲染主题不渲染正文；
任务只渲染标题；KB 没有内容；没有历史对话，没有 memory 读取。
这比"上下文有限"更糟——上下文几乎是空的，LLM 无法做出有意义的
决策。

需要：
- 邮件正文默认进入上下文（受 token budget 约束，超过则摘要+引用）。
- `AgentContinuation.event_log`（或独立 MemoryStore）应被编译进
  prompt，至少包含最近 N 轮 ReAct 摘要和未完成承诺。
- 工具定义应从 `ToolManifest.input_schema/output_schema` 自动生成，
  而不是 `PromptTemplates` 里手写的 6 个工具子集。

### 1.3 "实时运行 + 调慢时钟 + 人在 ReAct 轮注入意见"

现状：
- `Simulation.run()` 是同步紧循环，不睡眠、不响应外部控制；
- `HumanControl.set_tick_duration()` 写入 pending 队列，但
  `run_tick()` 从不调用 `apply_pending_duration_changes()`；
- 人类 Email 可进 MailSystem，但 Agent 收到后下一轮如何"请人
  拍板"没有专用通道；`OpType.HUMAN_DECISION` 枚举存在但无
  生命周期、无 UI 接入口；
- LLM op 没有 dispatcher：`_phase_dispatch` 只处理
  `TOOL_REQUEST`，真实 LLM 调用没有 worker 去执行。

需要：
- **SimulationRuntime**：一个带 wall-clock 的循环，tick 之间按
  `tick_duration` 睡眠，提供 start/pause/resume/step/set_duration，
  并在每个 tick 边界应用 pending duration changes。
- **Control Plane API**（HTTP/WebSocket）：查看状态、发 Email、
  暂停、审批工具、查看/编辑任务。
- **Async dispatcher**：一个 worker 进程（或线程池）轮询
  SUBMITTED 的 LLM/tool/human op，执行并 ingest 结果；与内核
  通过 `PendingOperationRegistry` 的持久化视图通信。
- **HumanDecision** 作为一等 op：Agent 提交 `RequestHumanDecision`
  intent → 进入 HUMAN_DECISION pending op → UI 展示 → 人类在
  任意 tick 注入决策 → 结果按 state_epoch fence 后唤醒 Agent。

### 1.4 邮件附件（未在 SPEC 中）

现状：Email 有 `metadata` 但没有结构化附件；没有跨 Agent 的
文件引用/传输机制；`send_email` 工具只接受 to/subject/body。

建议：
- `Email` 增加 `attachments: list[AttachmentRef]`，其中
  `AttachmentRef` = {ref_type: shared_kb|private_transfer|url,
  resource_id, path, version, hash, size, mime}。
- 私人文件附件用"发送时复制到共享中转区（system/transfer）+
  收件人只读授权 + 过期时间"；共享 KB 附件直接引用 path@version。
- 邮件正文渲染时附带附件清单；`read` 类工具可读取被授权附件。
- 对于小说工作室：章节文件作为附件在作者/编辑/审校之间流转，
  这是刚需。

### 1.5 知识库精细管理与自动注入

现状：SharedKB 是 path→content + version + permission + lock；
但**没有 `kb_read`/`kb_list`/`kb_search` 工具**，Agent 只能写
不能读；快照只有路径和版本，没有内容；没有按任务/邮件关键词
注入定义的能力。

建议：
- 增加 `kb_read`、`kb_list`、`kb_search`（可先做关键词/路径
  匹配，再演进到 embedding）。
- SharedKB 增加条目类型（document / glossary / style_guide /
  decision / report）与轻量元数据（tags, terms, owner_task）。
- ContextCompiler 集成 **KB Injector**：根据当前任务标题、描述、
  邮件正文中的术语，自动注入 glossary 条目；例如小说工作室的
  "世界观设定"、"角色圣经"，软件公司的"架构决策记录"。
- 注意权限：注入同样要过 PermissionEngine；只注入该 Agent 有权
  读取的条目。

### 1.6 用户作为 Worker Agent

现状：HumanControl 是控制平面，不是组织树节点；人类只能发
"外部邮件"，不能被 Manager 委派、不能在任务树中被分配任务。

建议：
- 在 AgentConfig 中增加 `agent_kind: llm | human | service`；
  Human Agent 的 runtime 是 UI 队列，而不是 LLM。
- Manager 委派给 human 时照常创建 task + email；human 收到任务
  后通过 UI accept/complete，这些操作翻译为与 LLM Agent 相同的
  Intent（CompleteTaskIntent 等），走同一事务路径。
- Human Agent 的"响应"就是一个 HUMAN_DECISION op，有 deadline、
  escalation、reminder 事件。

### 1.7 外部服务接入规范

现状：`ExecutorRegistry` 是进程内内存登记；`ToolRegistry` 的
handler 注册是 Python 闭包；没有 wire protocol、没有认证、没有
服务发现、没有健康检查。

建议制定 **Capability Provider Spec**：
- 外部服务提交 `ToolManifest`（版本+manifest_hash）+ 执行器
  端点；内核保留"注册即校验 + 默认拒绝"。
- 运行时通过持久化 op 表与外部服务交互：
  内核写 SUBMITTED `ToolRequest`（request_id/manifest_hash/
  input_hash/state_epoch/workspace_version）→ 服务认领 PENDING →
  回传 `ToolResultContract`。
- 传输层可先用 HTTP JSON 或本地 SQLite 轮询，后续换队列。
- 认证：executor token + 按 tool 授权；身份字段继续由内核注入。

---

## 2. 设计是否冗余？

### 2.1 僵尸组件（伪冗余）

以下模块独立测试通过、但主路径不经过它们，属于"看起来很完整"
的冗余：

| 模块 | 现状 | 建议 |
|---|---|---|
| `tick_engine.py` | 7 阶段 stub 引擎，每 tick 被空转一次 | 降为纯时钟，或删除阶段逻辑 |
| `executors.py` | 离散/微循环执行器，未接入 | 接入 Runtime，或标注为 v0.9+ |
| `identity.py` | IdentityEnforcer 未实例化；`validate_file_access` 为 pass | 接入 ToolContext 创建，或删除 |
| `file_ops.py` | FileOps 构造后未调用；写路径在 simulation 内联 | 主路径统一走 FileOps/PrivateStore |
| `human_control.py` | 命令 API 完整，但 tick 不应用 duration、不接 UI | 接入 SimulationRuntime |
| `delegation.py` | DelegationProtocol 仅暴露 property | 委派逻辑收敛到该模块，或删除 |

### 2.2 重叠的账本（真冗余）

现在至少有四本账：TransactionBuffer（本 tick effects）、
PendingOperationRegistry（异步 op）、Outbox（邮件投递）、AuditLog
（审计）。回滚时需要手工同步它们（P0-2 就是漏账）。

建议引入 **统一 TickJournal**（append-only）：
- 每个 tick 产生一个 `TickRecord`，内含：intents、effects、
  pending_op_registrations、outbox 条目、状态迁移、审计事件。
- Commit 成功 → journal 提交，各子系统从 journal 投影更新；
  Rollback → journal 标记 aborted，各投影自然回滚。
- Audit 成为 journal 的视图，而不是独立账本。

### 2.3 状态机制冗余

`AgentStateMachine`（生命周期状态机）与 `AgentContinuation`
（ReAct 恢复状态）大量重叠：两者都表达 waiting/processing，
但 continuation 才是真实驱动。建议合并为单一
`AgentRuntimeState`：`phase + pending_request + task focus + stats`，
保留少量生命周期状态（active/paused/terminated）即可。

### 2.4 阶段模型

10 阶段不是冗余，但实现要"名实相符"。当前 Ingest/Deliver 混在
Phase 1 边界、`last_tick_phases` 少 deliver、legacy 7 阶段并存。
建议统一为 SPEC §8.6 的 10 阶段，并把每阶段的输入/输出定义清楚。

---

## 3. 灵活性是否充足？

**不充足。** 主要卡点：

1. **工具不可扩展**：`builtin_manifests()` 和
   `_register_tool_handlers()` 都是写死的；新增"小说章节提交"或
   "代码评审"工具需要改内核。应有 `Simulation.register_tool
   (manifest, handler, executor=None, policy)` 公共 API，handler
   接收 `ToolContext` + 内核提供的 subsystem handles（受限）。
2. **LLM 工具定义硬编码**：`PromptTemplates.render_tool_definitions`
   只覆盖 6 个工具，且与 `builtin_manifests()` 手工同步。应自动
   从 manifest 的 input_schema/output_schema 生成 JSON Schema。
3. **观察形状硬编码**：无法为不同场景配置不同观察策略。
4. **场景配置不充分**：当前 config 只有 org tree 与基本参数；
   没有"场景包"概念（预置 org、工具集、KB 种子、提示词模板、
   人工节点）。软件公司与小说工作室应能以配置而非代码来定义。

---

## 4. 距离真正可用：两个场景的能力差距

### 4.1 软件开发公司模拟

已有：私有文件、apply_patch、run_tests、git_diff/git_status、
任务树、邮件。
还缺：
- 多文件补丁/代码检索（`code_search`、`multi_file_patch`）；
- 代码评审工具（`review_diff`、`comment`）；CI 结果接入；
- 任务依赖（B 阻塞于 A）与进度视图；
- `kb_read`/`kb_search` 注入架构决策与术语表；
- Email 附件（diff/报告）；
- 用户作为客户/工程经理 Worker；
- 成本/token 预算（P2-11）。

### 4.2 小说写作工作室模拟

已有：私有文件（章节）、write/read/ls、邮件、任务树。
还缺：
- 世界观/角色圣经 KB（`kb_read` + 术语自动注入是核心）；
- 章节附件流转与版本对比（`apply_patch` 可用于文本修改，但需要
  面向文档的 diff 展示）；
- 审校/评审流程（review request/result 的 email 类型已有，但
  prompt 中看不到正文，需要附件+正文渲染）；
- 风格一致性检查工具（可作为外部服务接入示范）；
- 用户作为作者 Worker，与编辑 Manager 协作。

---

## 5. 建议的目标架构（激进版）

```text
┌─────────────────────────────────────────────────┐
│  Control Plane (HTTP/WS UI)                     │
│  start/pause/step/slow-clock · 发 Email · 审批  │
│  人工任务队列 · 状态/成本/审计视图               │
└──────────────────────┬──────────────────────────┘
                       │ 命令/查询
┌──────────────────────▼──────────────────────────┐
│  SimulationRuntime                              │
│  wall-clock 循环 · tick duration · 事件总线     │
└──────────────────────┬──────────────────────────┘
                       │ run_tick
┌──────────────────────▼──────────────────────────┐
│  Kernel（10-phase，唯一阶段机）                  │
│  Ingest→Freeze→Schedule→Observe→Decide→         │
│  Validate→Act→Commit→Publish→Audit              │
│  统一 TickJournal                                │
└───────┬─────────────────────────────┬────────────┘
        │                             │
┌───────▼────────┐           ┌────────▼───────────┐
│ ContextCompiler│           │ Capability Layer   │
│ 按角色/task 编译│           │ ToolRegistry+      │
│ token-budgeted │           │ ExecutorRegistry+  │
│ briefing + KB  │           │ Provider Spec      │
└────────────────┘           └────────┬───────────┘
                                      │
                        ┌─────────────┼────────────────┐
                        │             │                │
                   内置工具      External Services    Human
                  (read/write/  (code_search/        (UI 队列)
                   patch/tests/ review/...)          (HUMAN_DECISION)
                   kb_read/...)
```

关键变化：
- 内核只保留状态提交与调度；**"给 Agent 看什么"外移到
  ContextCompiler**；"Agent 能做什么"外移到 Capability Layer。
- 所有异步操作（LLM、工具、人类决策）统一走
  PendingOperationRegistry，由 dispatcher workers 执行。
- 所有状态变更写入统一 TickJournal；PendingOps/Outbox/Audit 是
  其投影。

---

## 6. 建议路线（无向后兼容）

- **v0.9 — 把系统真正跑起来**：修复 P0-1/2/3；引入
  SimulationRuntime + Control Plane（最小 HTTP API）；实现 LLM
  dispatcher worker；合并/删除僵尸组件；统一 TickJournal 或
  至少把 pending op 纳入事务。
- **v0.10 — 上下文与能力**：ContextCompiler + ObservationPolicy；
  从 manifest 自动生成工具定义；公共 register_tool API；邮件附件；
  `kb_read/kb_list/kb_search` + KB 术语自动注入。
- **v0.11 — 人类与场景**：Human Worker Agent + HUMAN_DECISION
  生命周期；软件公司场景包；小说工作室场景包；外部服务接入
  规范（Provider Spec）。
- **v1.0 — 可用性**：成本/预算、审计回放、基于 journal 的
  确定性重放；为两个场景各写一个端到端 demo（从需求到交付，
  从大纲到成稿）。

---

## 验收标准（本 OPEN_ISSUE 关闭条件）

- [ ] 有明确的 ContextCompiler 设计文档，并回答了"每个层级
      Agent 看到什么形状的观察"（root/manager/worker 各一例）
- [ ] 有 Runtime/Control Plane 设计，说明 wall-clock、pause、
      slow-clock、人工注入的交互路径
- [ ] 有邮件附件与 KB 注入的设计说明（含权限与 token budget）
- [ ] 有外部服务接入规范草案（manifest 注册 + ToolRequest 认领
      + 结果回传 + 认证）
- [ ] 僵尸组件清单（第 2.1 节）每项都有"删除/接入"决定
- [ ] 统一 TickJournal 或等价方案被采纳或明确否决
