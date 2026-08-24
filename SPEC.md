# My-Team 多智能体协作框架设计 Spec

**版本:** v0.9.0 目标架构
**定位:** AI 辅助的一人公司（One-Person Company）运行框架
**前身:** v0.1.0–v0.8.0 实现细节保留在 `SPEC.v0.8.legacy.md`

---

## 0. 定位与目标

本系统用于运行一个**由单人所有、多智能体协作、面向真实业务场景的
"一人公司"**。人类是公司的所有者与最终决策人；AI Agent 是员工，
按协作网络分工（关系图 + 设备授权，§4.1）、异步协作、在事务化
时间步中推进工作。

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

### 0.4 术语与缩写约定

英文缩写首次出现时给出全称与定义；本文档关键缩写：

- **LLM**（Large Language Model，大语言模型）：本系统的推理引擎。
- **SLA**（Service Level Agreement，服务等级协议）：见 §9.2。
- **MCP**（Model Context Protocol）：见 §6.5。
- **KB**（Knowledge Base，知识库）：SharedKB 的简称。
- **API**（Application Programming Interface，应用程序接口）：见 §13。

后续章节使用缩写时不再重复全称；遇到未定义的缩写视为文档缺陷，
应在审阅中指出补齐。

---

## 1. 核心设计原则

1. **Tick 是提交单位，ReAct 是行为协议**：内核按离散 tick 推进，
   每 tick 状态提交一次；Agent 的思考-行动循环（ReAct）可跨多个
   tick。
2. **同一抽象水平思考**：观察由**关系与记忆**决定（§4.1/§5）——
   Agent 看到 superior 委派的、subordinate 回报的、collaborator
   发来的、设备状态，以及经记忆召回的上下文；不存在按角色名的
   观察裁剪。
3. **异步外部交互**：LLM、工具、人类决策、外部平台全部通过
   pending operation / ingress event 异步进行；任何 tick 阶段不得
   同步等待外部调用。
4. **默认拒绝，显式授权**：工具、平台、审批、知识库读取全部
   deny-by-default。
5. **人类是一等参与者**：人类可以是 Owner、Worker、Approver；
   人类任务与审批走与 AI 相同的事务路径。
6. **单一事实源**：所有状态变更写入统一 TickJournal；审计、回放、
   对账、恢复都是 Journal 的投影。
7. **三态内核（2026-08-24 收敛）**：系统分三类——**内核（纯逻辑，
   可带配置，零业务数据）**：时间引擎（tick/十阶段/事务回滚逻辑/
   审计逻辑）、效果级策略求值（deny-by-default/预算/epoch/身份
   注入）、**ACL 主体（role 本体：`{role_id: uuid4, name}`，零行为
   语义）**、执行真理（执行器分级/沙箱/受限解释器/锁原语）、认知
   真理（注入状态空间可重放）、Human UI 框架、闭包不变量校验；
   **设备（数据 + 读写工具 + ACL + 锁）**：基础设备（KB/邮箱/
   Record/Asset/Credential）、Task 设备、组织架构设备（positions/
   边语义/role 映射，边语义为 org 声明、内核只校验不变量）、世界
   记忆设备（Journal）、配置设备（role grants 与策略数据）、
   Ingress/Integration/MCP；**Agent（内心/头脑/双手的数据）**：
   记忆/continuation/私有工作区/身份。**内核是纯函数**
   `S' = K(S_devices, S_agents)`；org = core + devices + agents；
   业务场景不修改内核。
8. **ACL 主体 = role（2026-08-24 收敛）**：一切权限判定以 role 为
   主体——`有效权限 = role grants（岗位 roles 并集）∧ 设备 ACL ∧
   锁可用`；ACL 与授权数据引用 role，与组织架构解耦（组织重组不动
   ACL；岗人分离共享 role；细粒度 ACL——KB 页面级/任务级——同样
   引用 role）。业务标签（岗位名/display）不构成权限；直派形态
   （不经组织架构、直接指派 agent 的 role）为架构灵活性选项，框架
   不依赖组织架构存在。
8. **流程 = 知识，不是内核对象**（2026-08-24 决策）：业务过程以业务
   语言 SOP 文本承载、注入提示词；不设严格 ProcessDef 语法（无对应
   严格 runtime）。顺序/审批约束下沉为效果级策略（工具前置条件、
   requires_approval）与人类任务（HumanTask + Email 往返）。

---

## 2. 总体架构

```text
┌──────────────────────────────────────────────────────────┐
│                 Control Plane (HTTP/WS/UI)               │
│  启停/调速/单步 · 消息 · 审批台 · 工作台 · 看板 · 审计   │
└────────────────────────────┬─────────────────────────────┘
                             │ 命令 / 查询
┌────────────────────────────▼─────────────────────────────┐
│            SimulationRuntime (wall-clock 循环)           │
│  tick duration 生效 · pause/resume/step · 事件总线       │
└────────────────────────────┬─────────────────────────────┘
                             │ run_tick
┌────────────────────────────▼─────────────────────────────┐
│                 Kernel（10 阶段，唯一阶段机）            │
│  Ingest → Freeze → Schedule → Observe → Decide →         │
│  Validate → Act → Commit → Publish → Audit               │
│  统一 TickJournal                                        │
└───────┬───────────────┬───────────────┬──────────────────┘
        │               │               │
┌───────▼───────┐ ┌─────▼────────┐ ┌────▼──────────────────┐
│ContextCompiler│ │ Capability   │ │ Ingress/Egress +      │
│  角色化观察   │ │ 工具/执行器  │ │ Integration 适配器    │
│  KB/KPI 注入  │ │ 策略/审批    │ │ 外部平台事件与请求    │
└───────────────┘ └──────────────┘ └───────────────────────┘
        │               │               │
┌───────▼───────────────▼───────────────▼──────────────────┐
│ 数据层：PrivateStore · SharedKB · RecordStore ·          │
│         AssetStore · CredentialStore · MailStore         │
└──────────────────────────────────────────────────────────┘
```

### 2.1 组件职责

| 组件 | 职责 |
|---|---|
| Control Plane | 对外 API/UI；人类操作入口 |
| SimulationRuntime | wall-clock 循环；tick 调度；duration 变更 |
| Kernel | 10 阶段 tick；状态提交/回滚；Journal 写入 |
| ContextCompiler | 组装工作记忆（关系可见性 + 记忆召回 + 预算）；编排记忆整理模式（§5） |
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
| 2 | Freeze | 当前全局状态 | 提交态引用 + 目录/元数据索引（Index，不含文件全文）+ 按需路径级基准（lazy per-path freeze） | 本 tick 内所有 Agent 见同一提交态；读取按需合并自己的 staged；不构建全量内容快照 |
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
- **每 tick 一轮（唯一执行模型）**：每 Agent 每 tick 最多 1 次
  activation、1 次 Decide（最多 1 次 LLM 调用）；多轮推理是跨 tick 的
  ReAct 协议（continuation 续接），**不支持同 tick 内多轮 LLM→Tool
  micro loop**——它破坏提交原子性与读取一致性（曾存在于
  ExecutionMode 枚举，从未接线，正式废除）。Agent 在单轮内可执行
  多个内核工具（read/write/apply_patch），其余经 pending op 跨 tick
  投递结果。
- **原子提交的来源不是快照隔离，是串行化**：每 tick 一轮 + 互斥锁
  （SharedKB 层已有 LockManager；私有空间 per-agent 独占本无竞争）
  使并发访问串行化，Act 只登记/暂存、Commit 统一应用 → 天然原子。
  回滚粒度为 tick 起点（P0-2 continuation 快照 + 文件原状恢复）。
- **冻结视图按需化**：不构建"全体资源"内容快照（O(全部文件内容)
  每 tick 逐字复制是不可接受的成本）。Freeze 只建目录/元数据索引与
  状态摘要哈希（O(资源数) 的元数据，不含全文）；文件内容在 Agent
  实际读取的路径上按需读取（提交态 + 自己本 tick staged 的合并）。
  读取一致性由"每 tick 一轮 + 串行化"保证，无需快照隔离。

### 3.2 统一 TickJournal

> **归属（2026-08-24 三态收敛）**：Journal 的持久化与查询归
> **世界记忆设备**（数据层）；内核只含写入/回滚**逻辑**（§3.3）。
> 本节约定的是内核与设备的契约。

- 每个 tick 产生一个 `TickRecord`（append-only），包含：
  - 状态摘要哈希（epoch/提交集摘要，非全量内容快照）、epoch、tick；
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
- **回滚 = 逆操作，不用状态快照**（T18 定稿）：每个 EffectType 在
  `INVERT_CONTRACT` 声明其逆操作（记录什么前值 + 如何恢复）；提交时
  把前值写入 `StagedEffect.invert_data`（文件旧内容、KB 旧
  resource+version、任务旧状态、outbox entry_id 等）。回滚是单一
  入口，按应用顺序**逆序**逐 effect 执行逆操作：外部队列条目丢弃、
  文件/KB/任务写回前值、created 删除。已生效的不可逆副作用（邮件
  已交付、LLM 成本等）回滚时如实标 FAILED 并记审计，不静默吞掉。
- **失败分级**（用户 2026-08-18 定稿）：只有系统级不变量破坏（apply
  抛未预期异常）触发整回合回滚 + state_epoch 递增；可判定业务失败
  （权限/锁/版本/patch 冲突、重复 task_id、无效状态、缺失父任务）
  一律局部 FAILED，其余 effect 照常提交，不回滚世界。group 原子性
  在局部失败时同样成立：成员整体 FAILED，已应用的成员逐个逆操作
  撤销。
- 外部副作用不可回滚：LLM 成本、平台 API 已生效写入。此类操作
  必须声明 `reversible=false` 并走补偿/对账路径。

---

## 4. 核心实体

### 4.1 岗位（Position）与 Agent（协作网络）

组织架构是组织的内容，由**岗位（Position）**承载：`{position_id,
jd（职责/提示词）, edges, roles}`。Agent 被 hire 进岗位即
**自动继承**岗位的人际关系与设备授权。岗人分离：同一岗位可有多个
版本的 Agent 配置（不同基础信息/记忆/技能）用于评估——概念上动态，
实现静态先行（不做运行时换人策略）。

```python
Role:                            # 内核 ACL 主体（零行为语义）
  role_id: uuid4
  name: str                      # 可读名；权限绑定是数据（配置设备）

Position:                        # 组织架构数据（组织架构设备管理）
  position_id: uuid4
  jd: str                        # 职责/提示词：org 对 agent 的干预杠杆
  superior_id: uuid4 | None      # 直属上司岗位（唯一）
  subordinate_ids: list[uuid4]   # 下属岗位
  collaborator_ids: list[uuid4]  # 沟通合作者岗位
  roles: list[role_id]           # 多对多：岗位可具有多个 role（§1.8）

Agent:                           # 占据岗位的实体（模拟人的内心/头脑/双手）
  agent_id: uuid4                # 全局身份；显示名/标签可读
  kind: "llm" | "human" | "service"   # 运行模式（非权限依据）
  position_ref: uuid4 | None     # 占据的岗位（继承其边与 roles）
  llm_profile: str | None        # kind=llm（LLM 供应商是 Agent 内部结构）
  human_queue: str | None        # kind=human
  service_ref: str | None        # kind=service
  metadata: dict
```

- **ACL 主体 = role（内核实体）**：role 是一组权限绑定的命名主体
  （经典 ACL 语义：一个 role 对应多个 agent 的用户组语义）；
  岗位携带**多个** role（多对多，并集生效）——比一对一更灵活；
- **有效权限 = role grants（岗位 roles 并集）∧ 设备 ACL ∧ 锁可用**
  （§1.8）；grants（`role → (device, capability)`）与设备 ACL 条目
  都是数据，引用 role；**细粒度 ACL 同样引用 role**——KB 页面级
  （逐条目权限）、Task 级（同一任务对不同 role 可见/可改程度不同）；
- **业务标签不构成权限**：position.name/display 仅路由元数据；
- **直派形态**：不经组织架构、直接给 agent 指派 role 的
  "agent grants 设备"是合法替代（框架不依赖组织架构存在）——组织
  架构是默认形态，直派是朴素系统的选项；

- **权限 = role grants（岗位 roles 并集）∧ 设备 ACL ∧ 锁**（§1.8），
  全部动态注册；**边语义 = 组织架构设备声明的数据**（org 定义自己
  的边行为，一客一实例主权自治）；内核只校验**闭包不变量**（四条
  治理不变量：授权不授责 / veto 默认不可转授 / escalation 不转移
  ownership / 委派单调）。参考边语义（组织架构设备默认声明）：

| 边 | 方向 | 语义 |
|---|---|---|
| superior | 唯一入边 | 上报/escalation 对象；不可向它委派任务 |
| subordinate | 出边集合 | 可**命令委派**；下属不可拒绝，只能 fail |
| collaborator | 双向集合 | 可**请求委派**（请求帮忙），**可拒绝**（declined + 回执）；通信与共享上下文 |

- **委派模式**：任务携带 `delegation_mode = command | request`
  （见 §4.2）。command 的 accept 是确认（仪式性/UI 确认），request
  的 accept 才是真正的接受权；
- **升级 vs 回报**：escalation 严格沿 superior 边；collaborator 请求
  的失败/拒绝只**回报**请求方，不构成升级——以上均为组织架构设备
  默认声明的边语义，org 可改（不违反四条治理不变量）；
- **可见性由关系派生**（内核可见性规则 + token 预算，见 §5.2），
  无角色观察策略表；
- **经手物归属岗位**：task / report / mail 账号概念上属于 position
  （换人不换岗，活留岗上）；agent **无可持有资产**——身份、私密记忆、
  工作上下文之外一切归 org；
- **组织架构设备**：关系图/岗位/授权的读改写是一个设备——root 级
  agent 持有该设备权限即可做组织调整（运行时换人、多版本评估挂载）
  ——**动态优于静态**；
- **org 干预 agent 的唯一杠杆 = position 的 jd 提示词**（内心机制
  不可干预，见 §5.2）；role 标签仅路由元数据，非权限。

> 迁移说明：现行代码的 `AgentConfig`（role/tools 白名单/
> parent-children）与 ROOT_TOOLS/MANAGER_TOOLS/WORKER_TOOLS 硬编码
> 三档属旧模型，v0.11 岗位模型迁移到本模型。`kind=service` 兼作
> WorkerPool 的 manager 节点（§9.3）。

### 4.2 任务 Task

> **归属（2026-08-24 三态收敛）**：Task = **Task 设备**——任务树
> 是公共数据，**细粒度 ACL**（可见性按关系求值：同一任务对不同
> 岗位可见/可改程度不同）；任务 CRUD 与生命周期状态机 = 设备逻辑；
> 委派/升级的边语义校验在内核（对照组织架构设备的声明，§4.1）。

任务（task）是可追踪的工作陈述，由**责任**与**内容**两部分构成。

**责任**：
- `assigner`：委派方（谁分派这个任务）；
- `assignee`：责任方（谁承接/执行，对 assigner 负责）。assignee **不限定
  `kind`**——规则型（service）或 LLM agent 均可承担任务责任，责任人由
  任务字段声明，不由 agent 是否有推理能力决定（pool manager 语境见 §9.3）；
- `delegation_mode`: `command | request`（§4.1 边语义）——command =
  subordinate 命令委派（不可拒，只能 fail）；request = collaborator
  请求委派（**可拒绝**，进入 `declined` 态并回执请求方）。

**内容（任务书正文，不可变）**：标题、描述、验收标准（required_outputs）、
`deadline`（真实时间，见 §9.1）、`priority`、`depends_on: list[task_id]`
（执行前置：B 阻塞于 A，可先于执行期有声）、`artifacts`、`source_event_id`。
任务书一经生成不改写；任何需求变更 = 派生新任务，不改原任务书正文。

**委派 = 建副本（task materialization）**：assignee 向下委派时，即使内容
一字不改，也**新建一个副本任务**（新 task_id），在副本上改写
assigner/assignee 与（可选）deadline/验收；原任务书不变。责任随副本转移：
副本对它的 assigner 负责，形成逐层独立的责任关系（不依赖 agent 的 kind）。

**任务树 = 任务间引用关系的视图（不单独持久化）**：树不由独立数据结构
维护一致性，而是由副本间的**任务间引用**沿委派链动态推导：
- `derived_from: task_id | None`：本副本由哪一任务分解而来；
- （可由引用反向得出子级，用于组织/审计视图）。
每层 agent 通过其 briefing 上下文（专注任务、收件箱）感知分解责任；父任务
可对子级聚合状态（如 `WAITING_FOR_CHILDREN`），但**树的形状由引用关系导出，
不另建全局任务树数据**。

> 术语对齐（实现迁移）：责任字段已落地为
> `assigner_agent_id`/`assignee_agent_id`（2026-08-21，原
> `creator_agent_id`/`owner_agent_id`）；引用字段已统一——
> `parent_task_id` 并入 `derived_from`（2026-08-23；持久化
> SCHEMA_VERSION 1→2，v0.10 未发布，声明无跨版本存档兼容）。
> shared_kb 锁域的 `LockInfo.owner_agent_id` 是锁持有者概念，
> 不属责任链，保留不改。

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

## 5. 记忆与注入

Agent 的"看到什么"由**记忆系统**决定：长期记忆（条目列表）+ 工作
记忆（每次 LLM 请求的注入集）+ 召回 + 记忆整理模式。本节取代旧
ObservationPolicy/Briefing 模型（2026-08-24 重写）。

### 5.1 记忆条目（长期记忆）

记忆是**列表**，每条目由类型、内容、记忆点、关联对象四部分 + 元
数据构成：

```python
MemoryEntry:
  entry_id: uuid4
  type: task | skill | tool | person   # 预定义枚举
  title: str                           # 简短渲染用
  content: type-aware 结构             # 见下表
  memory_points: list[str]             # 触发器/索引，主动维护
  associated: list[uuid4]              # 关联对象：agent/设备/任务/业务 id
  version: int                         # 不可变版本链
```

- **记忆天生关系型**：每条记忆必有关联对象；id 一律走 `associated`，
  **不进 content**。无对象绑定的"自由知识"不存记忆，归设备（KB）。
- content 按 type 结构化：

| type | content 结构 | associated 语义 |
|---|---|---|
| task | 任务上下文笔记/进度/决策依据 | 业务/任务 id |
| skill | SOP 文本 + 适用条件 | 相关工具/设备 |
| tool | 受限 python 模组源码 + 入口 + 能力声明 | 设备 uuid |
| person | 档案、关系备注、偏好 | 对方 agent uuid |

- 写入：系统自动沉淀（任务/邮件/工具事件）+ Agent 主动管理（记忆
  工具），全部是 Journal effect；变更 = 新版本，永不原地改写。

### 5.2 工作记忆（注入集）

工作记忆 = **工作时预算范围内的上下文，直接影响下一次 LLM 请求**
（术语与认知心理学一致）。它不是存储类型，而是每次由召回组装的
注入集：

```text
工作记忆 = 召回(上下文词 ∪ 可控查询词 ∪ 临时覆盖) ∩ token 预算
```

- **可控查询词是状态**：Agent 可持久维护一部分查询词（注意力指针，
  可显示控制），属于注入状态空间（§5.5）；
- **主动回忆**：Agent 用 `memory_recall` intent 发起**临时召回策略**
  （一次性覆盖），延迟 1 tick 生效，复用异步基建（非阻塞）；
- **专注点**：`AgentContinuation.task_id` 为当前专注任务；切换任务
  需完成或转交（沿用 v0.6 专注点漂移控制）；
- **岗位 JD 注入**：position 的 `jd` 以 `[POSITION_JD]` 来源段注入
  工作记忆——**org 干预 agent 行为的唯一杠杆**（内心机制不可干预，
  §4.1）；
- 注入布局（注入哪些条目、顺序、详细度）与版本戳**入 Journal**。

### 5.3 召回

- 匹配：先关键词/子串匹配（触发器 vs 上下文词）；向量化是同一索引
  的**可插拔后端**——召回面 = 触发器列表（主动维护、可审计），
  **内容不向量化**；
- 命中 top-k 注入，受 token 预算约束；注入保留来源段标签
  （`[SKILL_INSTRUCTION]`/`[POLICY]`/`[UNTRUSTED_CUSTOMER_CONTENT]`，
  见 §11.4）。

### 5.4 记忆整理模式（CONSOLIDATING）

预算超阈值时（ContextCompiler 组装检测），Agent 进入**记忆整理模式**
——取代 harness 的固定总结提示词：

```text
触发：注入预算超阈值
进入：内核置 CONSOLIDATING 相位；工具面收窄为记忆工具集
输入：本次 LLM 请求允许以完整注入集为输入（目的就是让它变小）
动作：整理动作序列（全部 Journal effect）+ 一段极短摘要
退出：Agent 自决（exit 意图）或整理至阈值下
保证：整理期间不注入冗长正文 → 被打断的工作下一 tick 立即续上
```

记忆工具集（整理模式全开，平时可用子集）：`memory_fold`（折叠操作
历史/注入片段为浓缩条目）、`memory_promote`（提炼为长期条目）、
`memory_edit`、`memory_retag`（维护触发器）、`memory_evict`（移出
工作集、保留召回可达）、`memory_pin`（加入可控查询词）。

### 5.5 注入状态空间（可重放）

```text
状态 S   = (注入布局, 召回策略配置含可控查询词, 条目状态快照)
动力学   S_{t+1} = T(上下文_{t+1}, 策略调整(effect), 条目演化(effect), S_t)
```

- T 是确定函数；策略调整、主动回忆、条目写入/整理均为 Journal effect
  ⇒ **注入序列可从 Journal 重建**（"当时它看到了什么"可审计）；
- 控制面 = 三类 effect：调默认召回策略（持久）、主动回忆（临时）、
  条目管理（写/迁移/触发器/整理）。

---

## 6. 能力模型（工具与集成）

### 6.1 设备、ToolManifest 与 OperationPolicy

**设备（Device）**：资产 = 设备 + 数据。设备 = **工具包 + 数据**，
是带权限管理与锁机制的资源对象：基础设备（SharedKB、内部邮箱、
RecordStore、AssetStore、CredentialStore，§7）、**Task 设备**
（任务树公共数据 + 细粒度 ACL，§4.2）、**组织架构设备**（positions/
边语义/授权数据）、**世界记忆设备**（Journal 持久化与查询，§3.2）、
**配置设备**（引擎与策略配置数据）、Ingress/Integration（外部世界
设备）。**工具是设备的操作面**：工具 manifest 声明它操作哪个设备
（`device_id`）与能力（`capability`）。**设备依赖用接口定义**（如
邮箱设备依赖凭证设备接口）。**预算拆分**：LLM API 请求限额归 Agent
引擎内部管理（头脑的用度）；外部资源限额与 Ingress/Integration
设备一起管理（外部世界速率与背压）。

**授权单层化（2026-08-24 决策）**：ACL 主体 = **role**（内核实体，
§1.8/§4.1）；Agent 的有效权限 = `role grants（岗位 roles 并集）∧
设备 ACL ∧ 锁`。grants（`role → (device, capability)`）是配置设备
数据；**废除独立工具白名单**——旧 ROOT_TOOLS/MANAGER_TOOLS/
WORKER_TOOLS 与按名字的 `agent.tools` 不再存在。OperationPolicy
继续 deny-by-default（闭包）。

沿用 v0.8 并强化：

- `ToolManifest` 增加 `device_id`、`capability`、`approval_policy`
  （何时需要人工审批）、`ingress_event_types`（工具是否消费入站
  事件）、`egress`（是否外部出站）、`compensation_tool`（不可逆
  操作的补偿工具）。
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
- **已实现注记（2026-08-24，T16a/T16c）**：
  - T16a：`ExecutionClass.SANDBOXED_PROCESS` 落地——`run_tests` 升
    SANDBOXED_PROCESS，执行器 tier 为 SANDBOXED_OUT_OF_PROCESS；
    工具 manifest 声明 `SandboxConstraints`（rlimit CPU/内存/进程数/
    文件大小、环境净化 sitecustomize/PYTHON*/PATH/secret 剥离、
    GIT_* 固定、deny_network、只读挂载），隔离后端可插拔
    （`SandboxBackend`，决策 4），真实后端 = 可信 shim 子进程
    （rlimit → unprivileged userns 下 netns/mountns → execvpe），
    每约束实际应用与否进 `sandbox_report`，deny-by-default 不静默；
    `run_tests` cwd 为临时工作区副本（T17 by-product 宿主目录问题
    并入）。
  - T16c：token/cost 预算——定价表 + BudgetTracker 三作用域
    （agent/task/simulation）累计 request_count/token/cost/wall_time，
    跨 tick 持久化；PreValidate 预扫描超限（累计 + 本次估算）拒整个
    回合（与事务原子性一致），concurrency 同路径；审计
    `budget.rejected`。

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

- 平台适配器 = Integration；内核只认 Integration 契约；Integration
  归位为**设备**（出站能力 + ingress 消费，§6.1）。
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
- **安装框架**：MCP Adapter 是可执行能力包，经 `INSTALL_PACKAGE`
  审计安装（E5：如实申报 + 安装审计 + 审计员通知）；远程 HTTP 执行器
  （EXTERNAL_IRREVERSIBLE）的 unknown/对账语义挂接 pending-outbox-
  recovery（E3），幂等键用稳定键。

### 6.6 Human UI 系统与设备插件

- **Human UI 系统属内核**（2026-08-24 决策）：Control Plane 是
  Owner/人类的通用操作台（启停/消息/审批/审计/看板），是世界的
  一部分而非某设备；
- **设备可为其扩展前后端模块插件**：任一设备可注册自己的 UI
  模块（如组织架构设备的岗位管理页、KB 设备的知识编辑页），经
  设备接口声明，插件化挂载到 Control Plane。

---

## 7. 数据与存储

### 7.1 PrivateStore（私有工作空间）

- **归属：Agent 引擎内部机制**（§4.1/§5，每 agent 一个实例）——
  **非设备**：无公共数据、无协作锁、权限仅本人；设备定义见 §6.1。
- 路径解析统一走 `PrivateStore.resolve_path`；任何写路径必须先
  通过 resolve 与访问控制（修复 OI-003 P0-1）。
- 文件读经提交态视图（tick 提交态 + 自己本 tick staged 的按需合并，
  走 lazy per-path 读取，见 §3.1 冻结视图按需化）；文件写经 effect。

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
- （v0.10 T12b 已实现：env / 加密文件后端；内核只持 ref，dispatch 用
  value-free `has()` 门禁，`resolve()` 在 executor/plugin 边界解析。）

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
```

- Ingest 阶段消费；`(source, external_id)` 去重（持久化，跨重启）。
- 事件持久化成功后才向平台 ack（防丢）。
- **映射前门**：事件进入内核后统一走 `IngressEvent → 意图/任务`
  （唤醒相关 Agent，或由 manager 结合 SOP 知识分解为 Task），不再
  直接转 Record / Email 之外的对象；具体下游由被唤醒 Agent 决定
  （SOP 注入提示词，2026-08-24 决策：无 ProcessInstance）。
  v0.10 已交付方向中立的传输层：IngressBuffer / 去重 / ack /
  Integration 注册 / 出站 pending op；事件→任务的语义闭合属
  v0.11 任务治理绑定。

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

**时间模型（业务层 = 真实时间；底层引擎 = 离散时间 tick）**：
- **业务层一律真实时间**（wall-clock datetime），不论系统内部还是外部：
  `deadline`、`IngressEvent.occurred_at` / `deadline_hint`、cron 触发
  时刻全部是真实时间；Task/Email/外部承诺等业务层字段**不出现任何 tick
  概念**。
- **底层引擎 = 离散时间（tick）**：引擎每 tick 检查真实时钟、处理到期
  事件、执行一轮阶段；tick **对业务层完全透明、不可感知**，只驱动系统
  本身的推进速度（每 tick 对应的真实时间间隔 / 人类调速 / 暂停）。
- **到期判定**：引擎每 tick 用**真实时间直接比较**（真实时钟 ≥ deadline、
  cron 时刻已到）即触发；不存在 tick 化的业务时间字段（无
  `deadline_tick` 换算视图）。

- `ScheduleRule`：`{rule_id, target, cron | interval_ticks,
  next_run_tick, action}`（`next_run_tick` 为底层引擎的到期检查刻度，
  规则本身以真实时间/interval 表达）；
- 每 tick 评估，到期生成 TIMER_EXPIRY 事件或创建任务；
- 支持"每日发布""每周选题会""到期前 N tick 提醒"。

### 9.2 SLA 与优先级

**SLA**（Service Level Agreement，服务等级协议）：外部业务对任务的
**服务承诺**，由 `deadline`（真实日历时间截止，见 §9.1 时间模型）与
`priority`（重要等级）两个字段承载；调度按承诺等级排序执行，保证高
承诺任务先获得执行时间片。SLA 排序与激活容量的协同见 §14 抗超负荷能力。

- Task 携带 `deadline`（真实时间）与 `priority`；
- Schedule 阶段按 `(priority, deadline)` 排序就绪集（deadline 为真实
  时间，直接比较，无 tick 换算）；
- 到期前 `N` tick 生成 `DEADLINE_APPROACHING` 事件；
- 超时走结构化 escalation（`on` = unresolved | condition_breached |
  exception，`mode` = arbitrate | transfer | advise），不硬编码
  「通知 Manager → 转人工 → 关闭」阶梯。

### 9.3 WorkerPool

**决策 3（2026-08-19 定稿）：pool 不设独立一等原语，建模为协作网络上的
一个 `kind=service` manager + children + 声明式路由规则。** 选择动作本就
存在（LLM manager 复杂判定选人，service manager 读状态+规则选人是同一
动作的规则最简档），pool 只是该动作的退化形，不值得独立设计。立即/延迟
是同一 manager 的两种可配置行为（`routing` 配置项：`immediate | deferred`），
均不引入框架新实体；分配权在 manager 单点串行、同 tick 提交原子，无认领
竞态。由此：
- `DelegateIntent.recipient` 仍为 `agent_id`，指向该 service manager；
  **不引入 `pool_id`**。
- 删除 `AgentConfig.worker_pools`、Task `owner_pool` 等 Pool 专有机制。
- 路由配置 `AgentConfig.pool: PoolConfig {mode, strategy}`（仅
  kind=service 合法）：`strategy` = round_robin / least_busy /
  skill_match（内核执行的声明式规则，无 LLM 参与；`role`/`skill`
  标签仅为路由元数据，不构成权限依据）；`mode` =
  immediate（委派同 tick 展开为原任务+副本+通知的单组原子 effect）/
  deferred（任务先挂 manager，Ingest 每 tick 无状态推导「待分配×空闲
  child」原子分派）。分配权在 manager 单点串行、同 tick 提交原子，
  结果写入 Journal。

---

## 10. 人类参与与审批

### 10.1 Human Worker Agent

- `kind=human` 的 Agent 有任务队列；Manager 像对 AI Worker 一样
  委派任务；
- 人类通过 UI accept/complete/fail；动作翻译为 Intent，走相同
  事务路径；
- 人类任务有 deadline 与升级策略（超时提醒 Manager）。
- **已实现注记（2026-08-23，T12a）**：HumanWorkerRuntime（UI 队列
  驱动、空工具面）；UI 动作经 IngressEvent（source="human"）注入 →
  `_consume_ingress` 定向路由到 assignee → 翻译为 Intent 走标准事务
  路径；结构化 escalation（on/mode/target，post-commit 升级邮件 +
  审计；完整 escalation 机制归 v0.11 E1）。

### 10.2 审批与三查分离（统一为 HumanTask）

- 审批不建模为独立 pending op，而是 **HumanTask**
  （`kind = work | approval | decision | consultation`）：审批 =
  创建人类任务 → Email 通知 → 人类经 UI/邮件动作（IngressEvent,
  source="human"，T12a 路径）回应 → 结果决定续延方向。无编排层
  gate 对象（2026-08-24 决策，流程=SOP 知识）。
- 审批触发 = **效果级三查分离**：
  1. Capability：调用者能否调用（OperationPolicy，属闭包
     deny-by-default）；
  2. Authority：调用者是否有权作出该决策（Authority 裁决）；
  3. 审批态：效果所需的人类审批任务是否已决/未决（HumanTask 状态）。
  三者互不替代——`content.final` 不豁免 OperationPolicy 的 approval。
- 人类参与身份须经认证（Identity 闭包，见 §12.1：`from/to` 身份字段
  由内核注入，Agent 不可自指、不可伪造）。
- 审批有 deadline；未决/超时走结构化 escalation（`on`/`mode`/`target`），
  escalation 沿 **superior 边**（§4.1）；审批任务裁决者由关系图解析
  （superior 或指定 collaborator），不按 role。
- 审计记录谁在什么上下文批的。
- **版本切分**：v0.10 交付 Human Worker（kind=human Agent，§10.1）与
  CredentialStore（§7.5）；HumanTask 标准化与 Authority 接入属
  v0.11（任务治理绑定）。

---

## 11. 场景包

### 11.1 场景包结构（2026-08-24 对齐扩展表面）

场景包是**具备完全扩展能力的自包含安装单元**（2026-08-24 Owner
决策）：可携带声明配置与数据，也可自带可执行能力——包括以
ToolPlugin（§6.2）定义新工具、ingress adapter、受控脚本；引用既有
能力包仅是复用手段，不是定位限制。裁决权走 Authority 模型，不再有
独立审批策略文件。**流程 = 知识，不是内核对象**（2026-08-24 决策）：
业务过程以业务语言 SOP 文本承载（`sops/`），注入提示词，无
ProcessDef 语法。**信任模型为审计制（2026-08-24 Owner 决策）**：
无签名门槛、无分发期（一客一实例）；一切可执行内容以如实申报 +
安装审计 + 运行时约束承接，安全由审计员事后审查负责（见 §11.2）。

```text
scenario/
├── package.yaml                # package_id/version/api_compatibility/
│                               #   content_hash/installer/dependencies/
│                               #   capabilities_requested/namespace（E5 schema）
├── sops/                       # 业务语言流程 SOP（版本化知识，非内核对象）
├── org/relations.json          # 协作网络：superior/subordinates/
│                               #   collaborators + 设备能力授权（§4.1）
├── org/roles.json              # role 标签（仅显示/路由元数据，非权限）
├── authority/*.yaml            # AuthorityGrant 配置（8 域×strength×composition）
├── tools/                      # 自带工具：ToolPlugin 定义 / 受控脚本
│                               #   （须如实申报，审计制安装，§6.2/§11.2）
├── record_schemas/             # RecordStore 记录类型定义
├── ingress_mappings/           # 入站事件 → 意图/任务的声明式映射
│                               #   （adapter 可自带或引用，审计安装）
├── schedules.json              # 日历规则
├── kb_seed/                    # C 类数据：术语/规则/话术（内容默认不可信）
└── kpi/                        # root/manager 指标视图
```

相对旧结构的调整（2026-08-24 重划 + 定位修订）：

- `tools/` 为一等目录：自带 ToolPlugin 工装与受控脚本，或以全限定 ID
  引用既有能力包（`package_id:entity_type:entity_id@version`），
  两路平权；是否拆独立能力包仅为复用考量。
- `approval_policies.json`：被三查分离取代——Authority grants
  （域×强度×合成）+ 审批任务（HumanTask）+ OperationPolicy
  （requires_approval）。
- `scenario.json`：元数据并入 `package.yaml`。

### 11.2 安装与校验

- 安装 = `INSTALL_PACKAGE` 事务 effect，入 Journal，可审计、可回滚；
- 多阶段：Upload → Verify → Static validate → Stage → Prepare
  migration → Activate → Route；仅 Activate 改变运行时可见配置（E4）；
- 校验按条目类别分流：声明类条目过静态校验器（E7 五类基础 +
  权限单调/敏感数据流/资源上限）；**可执行条目要求如实申报**
  （capabilities_requested + 执行器分级），未申报的可执行条目加载
  即拒——这是审计完整性要求，不是信任门槛；
- ToolPlugin 定义的工具与内核内置工具**同权**（进程内注册，§6.2）：
  约束面 = manifest 如实申报 + 安装审计 + OperationPolicy
  deny-by-default；代码本体不做静态安全检查（查不了，由审计员
  事后审查承接）；
- **信任模型为审计制（2026-08-24 决策）**：无签名门槛、无分发期
  （一客一实例）。每次安装记录 installer / content_hash /
  capabilities_requested / 执行器分级入 Journal；能力类安装通知
  **审计员**（Owner 本人或其指定 kind=human 成员）事后审查；
- 运行时约束不因审计制放松：沙箱执行器分级、OperationPolicy
  deny-by-default、epoch fencing 照常生效；
- 拒绝 = 结构化错误列表 + **整体拒绝**（不半装）；
- 版本绑定 = **知识/策略快照戳**（2026-08-24 坍缩，无 ProcessInstance）：
  任务与决策记录其生效的 skill/tool/policy 版本；新 PackageVersion
  只影响新任务与新实例（运行中任务不换绑）。

### 11.3 五个场景包

| 场景 | 关键工具 | 关键 Record | 入站事件 | 人类角色 |
|---|---|---|---|---|
| 软件公司 | apply_patch/run_tests/git_diff/code_search | issue/pull_request | GitHub | 客户/工程经理 |
| 小说工作室 | apply_patch/kb_read/doc_diff | chapter/review | 无 | 作者/主编 |
| 电商 | kb_search/inventory_*/order_*/reply | sku/order/ticket/review | 平台消息/评价/订单 | 客服主管/审批人 |
| 自媒体 | content_plan/asset_*/publish_*/metric_* | content_asset/publish_job | 评论/数据回传 | 老板/终审 |
| 知识星球 | kb_search/content_calendar/member_*/post_* | member/subscription/post | 帖子/评论/会员事件 | 星主 |

> 注：「关键工具」可由场景包自带（ToolPlugin 定义）或引用既有
> 能力包，二者运行时地位相同。
> 五个场景包的资产实现属场景资产：最小测试向量端到端闭合后交付
> （v0.11 门：至少一个场景包 demo 运行且内核零改动）。

### 11.4 Skill Package（面向非专业用户的能力封装）

Skill 是一组"SOP + 提示词模板 + 知识 + 工具引用"的可装卸配置资产
（A 类），回答"Agent 如何做好一类具体工作"，面向不会写代码的个体户：

```text
skill/                          # A 类配置资产：只引用，不携带可执行能力
├── skill.manifest              # package.yaml 同构元数据 + frontmatter 触发条件
│                               #   + capabilities_requested（仅引用已有 Tool）
├── SKILL.md                    # 人读说明书：做什么、何时触发、SOP、边界；
│                               #   正文按结构化来源段组织（见下）
├── prompts/                    # 角色/任务提示词模板
└── kb_seed/                    # 可选：术语、话术、规则、模板（C 类数据）
```

相对旧结构的删除（重划决策，2026-08-24）：

- `scripts/`：脚本是可执行能力，默认独立成能力包（复用与注入面
  收敛考虑，结构约定而非安全强制）；Skill 以稳定全限定 ID 引用既有
  Tool/脚本，不自带；内嵌须如实申报并同等对待（§11.2，E5）。
- `approval.json`：动作是否须人工审批由 OperationPolicy
  （requires_approval）与审批任务（HumanTask/Authority）决定，
  Skill 无权声明审批策略。
- `tools/`：同 `scripts/`，只引用不携带。

提示词注入隔离（落到 ContextCompiler）：

- SKILL.md 正文按结构化来源段组织：`[SKILL_INSTRUCTION]` / `[POLICY]` /
  `[UNTRUSTED_CUSTOMER_CONTENT]`；
- 注入 briefing 时保留来源标签并规定优先级：POLICY（来自内核与 Owner）
  不可被 SKILL_INSTRUCTION 覆盖；客户内容永不作为系统指令，只能作带
  标签的数据上下文；
- 触发注入（关键词/任务类型/角色命中）在 token budget 内进行，注入
  内容必须带来源与信任等级。

权限边界：Skill 引用的工具/KB 仍受 PermissionEngine 与 OperationPolicy
deny-by-default 约束；Skill 不能自行提升权限。Skill 与场景包可组合：
场景包可引用多个 Skill。首个示例 Skill 属场景资产，待最小测试向量
闭合后交付。

---

## 12. 安全与不变量

1. **身份**：Agent 身份为全局 uuid4（§4.1）；ToolContext 只由内核
   创建；from/to 身份字段（uuid4）由内核注入，Agent 不可自指、不可
   伪造（Identity 闭包，P1 落地）。
2. **路径**：一切写路径经 PrivateStore.resolve_path；拒绝 `..`、
   绝对路径越界与 symlink 逃逸。
3. **权限**：权限 = **role grants ∧ 设备 ACL ∧ 锁**（§1.8），全部
   显式、动态注册；不存在按业务标签授权的路径。
4. **凭证**：引用式存储，不进 Journal/审计/prompt（T12b 已实现，
   可观测面有测试断言）。
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

GET  /org/graph               协作网络（关系图）
GET  /org/positions           岗位清单（JD/边/授权）
PUT  /org/positions/{id}      编辑岗位（JD/边/授权；组织架构设备）
POST /org/agents/{id}/mount   岗位挂载/换人（岗人分离）
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

## 14. 抗超负荷能力（Capacity & Overload Resilience）

系统在多维度设有限额，超限时统一遵循：**宁可排队、不可丢失；可背压、
必可解释**。超限绝不导致崩溃或静默丢任务。所有限额参数均为配置项，
上限的物理含义 = 系统在该维度的真实负荷能力（模拟并发、LLM 网关、
子进程、外部平台配额、存储）。

### 14.1 维度清单

| 维度 | 限额参数 | 超限行为 |
|---|---|---|
| 激活（调度） | `max_active_agents_per_tick`（T11 引入） | 容量内按 `(priority, deadline)` 选激活（deadline 为真实时间，直接比较）；超容者保持就绪，下 tick 再竞争（幂等，无状态损失） |
| LLM 并发 | `max_concurrent_llm_requests` | 请求级背压：超限保持 SUBMITTED 排队，不拒绝 |
| 每激活预算 | `max_llm_calls_per_activation` / `max_tool_calls_per_activation` / `max_action_budget` | PreValidate 拒绝（非背压；可解释，不改状态） |
| 工具并发 | executor `max_concurrent` | 工具级背压：capacity 压力下 op 保持 PENDING 排队（retryable） |
| 外部平台 | `Integration.rate_limits.max_calls` | provider 闸背压：保持 SUBMITTED，下 tick 再试（T9 两因独立性） |
| 执行超时 | manifest `max_runtime_ms` | 超时杀进程组，op 置 TIMED_OUT |
| 重试 | outbox `max_retries` | 达上限转失败/人工路径 |
| 存储 | `private_storage_limit_mb` | 写拒绝（可解释） |
| 输出 | `max_output_bytes` | 截断（不丢任务，只丢细节） |
| 委派深度 | `max_delegation_depth` | PreValidate 拒绝（防递归失控） |

### 14.2 语义分层

- **背压（retryable）**：容量/配额类超限——op 保持排队状态，下 tick
  自动再竞争；executor 容量与 provider 配额各自触发、互不混入
  （T9 两因独立性：`放行 := executor_admitted ∧ provider_admitted`）。
- **拒绝（非 retryable）**：预算/深度/合法性类超限——PreValidate 拒绝，
  给出 reason，不改状态。
- **截断/超时**：资源保护类——不丢任务，只降输出细节或终止单个执行。

### 14.3 协同顺序（级联减压）

激活名额（调度层）→ LLM 并发（请求层）→ 工具并发（执行层）→ 外部
配额（平台层）：上层名额不足时，下层不产生请求，逐级减压；任一层
超限只影响该层，不升格为整 tick 失败（T18 业务失败分级）。

---

## 15. 关键不变量（验收级）

- [ ] 一个 tick 只产生一个 TickRecord；回滚 tick 不产生 dispatch。
- [ ] 任何 pending op 都有明确的创建 tick 与终止 tick；不存在
      跨 tick 孤儿 op。
- [ ] 文件写路径不可能逃逸 Agent 私有空间。
- [ ] 回滚后 Agent 状态/Continuation 与 tick 开始前一致。
- [ ] `run_tick` 返回的 phases/committed/errors 与 Journal 一致。
- [ ] 每个场景包加载后，内核代码不变。
- [ ] 外部事件 `(source, external_id)` 跨重启去重。
- [ ] 审批通过前，高风险工具不得执行。
- [ ] 连续内核级崩溃（滑动窗口内达阈值）自动暂停（reason=crash_guard）
      且仅人工 resume；业务失败（局部 FAILED，T18 分级）不计为崩溃。
- [ ] 权限 = role grants ∧ 设备 ACL ∧ 锁；不存在按业务标签授权的
      路径（§1.8/§4.1）。
- [ ] 任一 Agent 的注入记忆序列（工作记忆布局 + 版本戳）可从 Journal
      重建（§5.5 可重放性）。
- [ ] 组织架构声明的边语义不违反四条治理不变量（静态校验，N8）。

- **已落地注记（2026-08-24，T16a/b/c）**：
  - `run_tests` 在真实沙箱（只读挂载 + 网络拒绝 + 资源限制 + 环境
    净化）下执行，非自证测试实证（网络真拒 / 环境真净化 / 资源
    限制真生效 / 只读挂载 EROFS）；
  - 超 token/cost 预算的 LLM 请求在 PreValidate 拒绝（拒整个回合）；
  - Snapshot 覆盖矩阵（tests/test_snapshot_matrix.py）：10 类状态面
    × 3 性质（Freeze 可见性 / Commit 可回滚性 / 持久化）= 30 行
    逐行断言 + 完整性自检，覆盖本清单各不变量（回滚一致性、
    pending op 生命周期、去重、状态持久化）；矩阵发现并修复
    REMOVE_CREATED 逆操作悬空子边缺陷。

---

## 16. 版本路线图

- **v0.9 — 内核可信与真实运行**：P0 修复、统一 Journal、
  Runtime/Control Plane、LLM dispatcher、ContextCompiler 最小版。
- **v0.10 — 边界能力**：Ingress/Egress、RecordStore、AssetStore、
  ToolPlugin API、KB 读取与附件、Calendar/WorkerPool、
  ApprovalGate、Human Worker。
- **v0.11 — 扩展表面 P0 语义闭合 + 场景资产**：设备化与单层授权
  （基础设备归位 + Task 设备 + 世界记忆设备 + 配置设备）、岗位模型
  （positions/边语义/组织架构设备）、记忆与注入系统、任务治理绑定
  （HumanTask/Authority/escalation 归一）、恢复与对账、资产边界
  （审计制）、谓词分级、静态校验器；场景包/Skill/MCP 在语义闭合后
  交付（最小测试向量端到端闭合，内核零改动）。
- **v0.12 — 真实 LLM API 接入验证**：llm_gateway 对接真实供应商
  （OpenAI 兼容 / Anthropic / DeepSeek 等），端到端验证
  （async 续延 + 工具调用 + 失败重试 + 限额记账），生产配置
  化（模型/密钥经 CredentialStore）。
- **v0.13 — 沙箱脚本执行器（bash）**：受限执行器族扩展
  （python 模组 + bash 脚本，同 T16a 沙箱）；脚本执行模型 +
  长驻会话复用 + max_processes 约束；v1.0 前必做。
- **v1.0 — 一人公司可用**：成本/预算、审计回放、确定性重放、
  五场景端到端验收；README 面向个体户的安装/托管路径。

---

## 17. 与旧版 SPEC 的关系

本文件是 v0.9 目标架构 Spec，取代 SPEC.v0.8.legacy.md 作为当前
设计权威。旧版保留为 v0.1–v0.8 实现的历史记录。迁移过程中，
v0.8 已实现的机制（ToolManifest、OperationPolicy、PendingOps、
Outbox、SQLite 持久化、L0/L1 Python 执行）继续有效，但按本 Spec
逐步重构到统一 Journal 与插件化边界层。
