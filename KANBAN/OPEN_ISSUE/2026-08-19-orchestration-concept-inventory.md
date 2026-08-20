---
kind: issue
status: open
source: SPEC §9 调度 / §4 核心实体 / §3 内核 / §14 抗超负荷
phase: v0.10
priority: high
---

# Orchestration 概念盘点（管理学 / 协作层全览）

**Opened:** 2026-08-19
**Status:** OPEN — 概念清单与缺口分类，作为 T11/T12 与 v0.11 排期的公共参照

## 目的

把系统目前用到的所有管理学 / orchestration 概念做一次分层盘点，
三态标注：**✅ 已实现**（代码里有）／**⏳ 已设计**（SPEC 定了，排在
v0.10-11，欠实现不欠决策）／**❓ 未定**（真正设计空白，需要决策）。
区分「已定未做」与「真正缺口」，不把 roadmap 排队混成缺陷。

## A. 谁做 —— 组织与委派层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| 组织树 | ✅ | `agent_tree.py`：静态父子树，parent/children/可委派给直接子级；含环检测 | 无 |
| 委派 DelegateIntent | ✅ | `intent.py`：分派到 `recipient_agent_id`（**仅限人**） | 决策 3 明确**不引入 `pool_id`**（pool = service manager 节点，仍委派到 agent_id） |
| AgentConfig.kind | ❓ | SPEC §4.1 规划 `llm\|human\|service` | **代码无 `kind` 字段**——Human Worker(T12a) 前置 |

## B. 做什么 —— 任务层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| 任务书（不可变）+ 状态机 | ✅ | `task.py`：任务状态机（DRAFT→…→COMPLETED）+ 生命周期状态（IN_PROGRESS 等） | — |
| 委派 = 副本 (task materialization) | ⏳ | §4.2 定稿：委派即新建不可变副本（新 task_id）+ `derived_from` 引用；assigner/assignee 声明责任（不限 kind） | 代码仍 creator/owner + 单任务可变，未迁移 |
| 任务树 = 引用视图 | ⏳ | §4.2 定稿：树不单独持久化，由副本引用链动态推导 | 未实现（现用 parent/child 字段维护） |
| 任务依赖 depends_on | ⏳ | §4.2 执行前置（B 阻塞于 A），与"分解建树"正交 | 代码 Task 无此字段 |
| SLA | ⏳ | §9.2 = deadline(真实时间)+priority | `deadline_tick` 未迁移（`task.py`/`intent.py`） |

## C. 何时做 —— 调度层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| AgentScheduler | ✅ | `scheduler.py`：事件驱动 ready set + WakeCondition；每 tick 每 agent≤1 激活；claim/defer/consume/requeue(回滚) | ready set 按 `agent_id` 排序，**无 (priority,deadline) 排序**、无容量上限 |
| 激活容量 | ⏳ | §14.1 `max_active_agents_per_tick`（T11 决策 2 已定） | 未实现 |
| Calendar / ScheduleRule | ⏳ | §9.1 cron 子集（日/周；决策 4 已定真实日历） | **代码零实现** |
| WorkerPool | ⏳ | §9.3 决策 3 已定：pool = `kind=service` manager + children + 声明式路由（round_robin/least_busy/skill_match）；立即/延迟为 `routing` 配置项，无独立原语、无 `pool_id` | 零实现；前置需落地 `kind=service` 字段 |

## D. 谁能做 / 需批准 —— 决策权限层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| Authority | ✅ | `authority.py`：8 domain、AuthorityGrant 7 元组、4 治理不变量；Escalation 完备(on=unresolved/condition_breached/exception, mode=arbitrate/transfer/advise) | — |
| ApprovalGate / HumanTask | ⏳ | §10.2 统一为 HumanTask + 三查分离（Capability/Authority/Gate） | v0.11；现 approval 散落 tool_manifest/simulation，非统一模型 |
| Human Worker | ⏳ | §10.1 kind=human + UI 队列 | v0.10 T12a；前置缺 `kind` 字段 |

## E. 执行健康 —— 韧性层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| 失败分级 | ✅ | T18：业务失败→局部 FAILED；系统级破坏→整 tick 回滚 | — |
| 抗超负荷 10 维度 | ⏳ | §14 背压/拒绝/截断分层 | 多数未实现 |

## 缺口性质分类

**ⓐ 已定未做（欠实现不欠决策，按 roadmap 排队）**：`deadline_tick`→真实时间
迁移(B)；激活容量(C)；Calendar(C)；WorkerPool = service-manager pattern 落地
(C，前置 `kind=service`)；`kind` 字段(A)；依赖 depends_on(B)；ApprovalGate(D)；
Human Worker(D)。

**ⓑ 真正设计空白（需决策）**：**已空**——WorkerPool 路由语义（决策 3）已于
2026-08-19 定稿：pool = service manager + children + 声明式路由，立即/延迟为
`routing` 配置项，无独立原语、无 `pool_id`。（溯源佐证：OI-005/OI-006 原始客服
场景要的是委派到池、按负载/技能选中，即立即语义；认领竞态被 manager 单点串行
消解。）

**ⓒ 潜在偏离/扩张（非缺陷，需显式定位）**：WorkerPool 的定位。与 Email/组织架构
核心的边界判断——
- 组织树 / 委派 / Email：**点对点、少而精、可精确指定人**（组织架构建模）；
- WorkerPool：**量大、同质、可互换、不关心是谁**（客服运营建模）。
决策 3 将此偏离**消除**：pool 不再是独立选址原语，而是「组织树上一个
`kind=service` manager + children」——委派仍是点到该 manager 节点，邮箱投递之外
的第二种选址逻辑不进入 Schedule/Commit（分派是 manager 内部行为）。

## 交叉引用

- `KANBAN/TODO/scheduler-calendar-pool`（T11，决策 3 已定，待开工）
- `KANBAN/TODO/human-worker`（T12a）
- `KANBAN/OPEN_ISSUE/extension-surface-spec`（OI-005/006 出处：Authority/编排层/池）
