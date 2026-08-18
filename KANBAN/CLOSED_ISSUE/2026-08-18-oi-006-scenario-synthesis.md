---
kind: issue
status: closed
source: 应用场景四：社交平台自媒体（抖音/小红书/B站）；应用场景五：知识星球运作；结合场景一/二/三
priority: high
---

# OI-006: 场景驱动的架构补充 — 自媒体与知识星球，以及五场景抽象收敛

**Opened:** 2026-08-17（OI-005 之后，新增场景四/五并做横向收敛）
**Status:** CONVERTED — 已拆分为 TODO（v0.10/v0.11）
**Converted to TODO:** 2026-08-17-scheduler-calendar-pool.md, 2026-08-17-record-and-asset-store.md, 2026-08-17-scenario-packages.md

---

## 0. 结论摘要

自媒体和知识星球两个场景没有推翻前几轮的架构判断，反而让"通用
内核 + 场景包"的路线更清晰。它们新增暴露了两个此前未识别的通用
缺口：

1. **日历/循环任务（Calendar/Recurring Scheduler）**：自媒体要
   "每日发布/每周选题会"，知识星球要"每周直播/每月总结"。当前
   调度器只有一次性 WakeEvent，没有周期性任务生成器。
2. **外部联系人/会员（Contact/Member）与二进制资产（AssetStore）**：
   自媒体要管图片/视频，知识星球要管会员/订阅；当前系统只有
   Agent 与文本文件，私有文件快照直接跳过二进制。

加上 OI-004/005 已识别的缺口，五个场景共同指向的通用原语已经
收敛。内核不需要再大改，需要的是**把边界层补齐**，并用"场景包"
证明灵活性。

---

## 1. 场景四：社交平台自媒体矩阵

### 典型流程

```text
热点/趋势采集 → 选题会 → 脚本/大纲创作 → 制作(文案/视频/图片)
→ 内部审核 → 平台发布(定时/即时) → 评论/私信互动
→ 数据回收(播放/点赞/评论/涨粉) → 复盘与策略调整 → 下一轮选题
```

### 与现有架构的差距

- **日历与循环任务**：每日发布、每周复盘、热点监测轮询都是
  周期性任务。当前 `AgentScheduler` 只有一次性事件；
  `WakeEventType.TIMER_EXPIRY`/`RETRY_TIMER` 存在但无生成者。
  需要 `ScheduleRule`（cron 式或 tick 间隔式）→ 每个 tick 评估 →
  生成定时唤醒事件或创建任务。
- **二进制资产**：视频、封面、图片、剪辑工程。`_build_snapshot`
  对私有文件视图跳过二进制（`UnicodeDecodeError` 时 continue），
  `read` 工具只能读 UTF-8 文本。需要 `AssetStore`：内容寻址的
  blob 存储 + 元数据（mime/size/hash/duration/cover）+ 引用。
- **多平台发布与回传**：抖音/小红书/B站各有 API、内容格式、
  审核规则、限流。这直接复用 OI-005 的 `Integration` 抽象，但
  发布类工具是 `EXTERNAL_IRREVERSIBLE`，需要幂等、状态回查、
  定时发布（publish_at）、失败补偿。
- **数据 KPI**：内容总监（Root）需要跨平台数据看板（播放、
  互动、转化、粉丝增长），而不是任务列表。ContextCompiler 要
  能从 RecordStore/Metrics 投影生成 KPI briefing。
- **评论/私信运营**：与电商客服同构但量更大、低危更多。可以
  复用 OI-005 的 Ingress + 客服式 WorkerPool，但需要批量规则
  自动回复、高情绪转人工、评论区风控。
- **内容审批**：发布前的人类/负责人审批（尤其恰饭广告、敏感
  选题）。直接复用 ApprovalGate。

---

## 2. 场景五：知识星球运作

### 典型流程

```text
内容日历(每日更新/每周直播/每月总结) → AI 草拟内容/答疑
→ 星主(人类)审批 → 发布到星球 → 会员提问/评论/打卡
→ AI 分类与草拟回复 → 高风险转人工 → 会员管理(续费/流失/分层)
→ 数据复盘(活跃/收入/留存) → 调整运营策略
```

### 与现有架构的差距

- **外部联系人与会员记录**：知识星球服务的对象是"会员"而不是
  Agent。当前系统只有 Agent 和任务，没有"外部联系人"实体。
  需要 `Contact`/`Member` 记录类型（昵称、平台 ID、订阅状态、
  到期日、标签、活跃度），并可关联 Ticket/帖子/订单。
- **订阅/续费/财务记录**：会员状态、付费、退款。这是 OI-005
  中 RecordStore 的直接应用，但要求记录类型可扩展到会员与订阅，
  且具备到期事件（到期前 N 天生成续费提醒/流失预警）。
- **社区内容审核**：星球帖子/评论需要审核。这是"入站内容 →
  分类 → 自动放行/人工审核/删除"的管线，与恶评检测同构。
- **星主即 Human Worker**：星主是内容与商业决策的最终负责人，
  最自然的形态是"人类作为组织树中的 Worker"，被 AI Manager
  委派、有任务队列、可审批/创作/驳回。这正是用户设想的
  "用户作为 Worker Agent"的最强场景。
- **内容日历与知识库**：星球的核心资产是知识。需要 `kb_read`/
  检索/术语注入（OI-004），以及按日历循环产出的内容模板。

---

## 3. 五场景横向对比与抽象收敛

| 能力 | 软件公司 | 小说工作室 | 电商 | 自媒体 | 知识星球 |
|---|---|---|---|---|---|
| 内部任务/委派 | 强 | 强 | 强 | 强 | 强 |
| 文件/文档 | 强（代码） | 强（章节） | 中 | 中（脚本） | 强（内容） |
| 二进制资产 | 中（产物） | 弱 | 弱 | **强（视频/图）** | 弱 |
| 外部入站事件 | 弱（GitHub） | 弱 | **强（消息/评价/订单）** | **强（评论/数据）** | **强（帖子/会员）** |
| 外部出站 | 中（git） | 弱 | **强（平台 API）** | **强（发布/回复）** | 中（发布/回复） |
| 结构化记录 | 中（issue） | 弱 | **强（库存/订单）** | 中（内容数据） | **强（会员/订阅）** |
| 日历/循环 | 中（迭代） | 弱 | 弱 | **强（每日发布）** | **强（每日更新/直播）** |
| 人工审批 | 中（review） | 强（编辑） | **强（退款/回复）** | **强（发布前）** | **强（星主）** |
| KPI 看板 | 中 | 弱 | 强 | **强** | **强** |
| 用户即 Worker | 中（客户） | 强（作者） | 强（主管） | 中（老板） | **强（星主）** |

结论：五个场景对内核的要求基本一致；差异主要在**场景包内容**
（工具、记录 schema、入站适配器、日历规则、审批策略、观察模板、
KPI）。因此目标架构应坚持：

> **一个事务化 tick 内核 + 一套通用边界原语 + 五个场景包。**

### 3.1 通用内核原语（收敛清单，无向后兼容）

1. **Unified TickJournal**：所有 effect/op/事件/审批/记录变更的
   唯一事实源；审计、回放、对账从 journal 投影。
2. **Ingress/Egress**：外部平台事件的入站缓冲（去重/排序/ack）
   与出站请求（幂等/回查/补偿）。Email 与 HumanMessage 只是
   Ingress/Egress 的特例。
3. **RecordStore**：类型化记录 + 不变量 + ledger 投影。覆盖
   库存/订单/ticket/会员/订阅/内容指标。
4. **AssetStore**：二进制 blob（内容寻址）+ 元数据 + 引用；
   私有文件与 Email 附件都基于它。
5. **Calendar Scheduler**：一次性事件 + 循环规则（cron/interval）
   + SLA 优先级排序 + `DEADLINE_APPROACHING` 生成。
6. **WorkerPool 路由**：委派目标从"指定 Agent"扩展为
   "Agent | Pool | 技能/负载策略"。
7. **ApprovalGate**：`requires_approval` 产生 HUMAN_APPROVAL op，
   UI 审批，批准后继续执行。
8. **ContextCompiler**：按角色/task/calendar 编译 token-budgeted
   观察；注入邮件正文、任务描述、KB 术语、KPI 看板、相关记录。
9. **Runtime / Control Plane**：wall-clock 循环、慢时钟、暂停、
   单步、控制 API、审批台、客服/运营工作台。
10. **CredentialStore**：平台/LLM 凭证引用式存储，不进 journal、
    不进审计、不落 DB 明文（可外部 KMS/env）。

### 3.2 场景包内容（配置 + 插件，不改内核）

```text
scenario/
├── org_tree.json          # 组织树（可含 human worker）
├── roles.json             # role → 观察策略/提示词/工具/KPI 面板
├── tools/                 # manifest + handler/executor 引用
├── record_schemas/        # sku/order/ticket/member/subscription/
│                          #   content_asset/metric 等
├── ingress_adapters/      # 平台 webhook/轮询适配器
├── schedules.json         # 日历规则
├── approval_policies.json # 哪些工具/金额/情绪需人工审批
├── kb_seed/               # 话术、术语表、规则、世界观
└── kpi_dashboards/        # root/manager 的指标视图
```

### 3.3 对"设计是否冗余"的最终判断

- **内核不是冗余，是底座**；五场景都需要它。要解决的是
  OI-003/004 指出的"僵尸组件"与"多账本"冗余。
- **边界层不是冗余，而是缺失**；当前系统把 20% 的边界逻辑
  内联在 `Simulation` 里（工具 handler、邮件、快照），却把
  80% 的边界需求（入站、记录、资产、日历、审批）遗漏了。
- 因此重构方向不是"给内核加更多阶段"，而是"把 Simulation
  拆薄"：内核只做调度与提交，边界能力全部插件化。

---

## 4. 距离真正可用（五场景版）

按依赖顺序：

1. **P0 修复 + Journal**（OI-003）：路径穿越、pending op 事务、
   TickResult 真相；统一 journal 落地。
2. **Runtime + Dispatcher**（OI-004）：wall-clock 循环、LLM 与
   工具的异步 worker、控制 API。
3. **Ingress + RecordStore + AssetStore**（OI-005/006）：外部
   事件、结构化记录、二进制资产三大边界能力，先做最小闭环。
4. **Calendar + WorkerPool + ApprovalGate**：周期性任务、池路由、
   真实人工审批；这是五个场景的"运营感"来源。
5. **ContextCompiler + KPI**：角色化观察与指标看板；这是用户
   最初"同一抽象水平"设想的落地。
6. **五场景包**：每个场景 = org + 工具 + record schema + 适配器 +
   日历 + 审批策略 + KB 种子 + KPI；先软件/小说（内部型），
   再电商/自媒体/知识星球（外部型）。
7. **v1.0 验收**：每个场景各跑通一个端到端 demo，且场景包
   安装不需要改内核代码。

---

## 验收标准（本 OPEN_ISSUE 关闭条件）

- [ ] 五场景能力对照表被确认或修订
- [ ] 通用内核原语清单（3.1）被确认或裁剪
- [ ] 场景包结构（3.2）被确认，并有一个最小示例包
- [ ] Calendar/AssetStore/Contact 三类新抽象有设计说明
- [ ] OI-004/005/006 合并为一份目标架构文档（SPEC v0.9 或
      独立 ARCHITECTURE.md）
