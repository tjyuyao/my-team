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
| 委派 DelegateIntent | ✅ | `intent.py`：分派到 `recipient_agent_id`（**仅限人**） | 无 `pool_id` 分支 |
| AgentConfig.kind | ❓ | SPEC §4.1 规划 `llm\|human\|service` | **代码无 `kind` 字段**——Human Worker(T12a) 前置 |

## B. 做什么 —— 任务层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| 任务树 + 状态机 | ✅ | `task.py`：完整状态转换（DRAFT→…→COMPLETED），priority、父/子、WAITING_FOR_CHILDREN | — |
| 任务依赖 depends_on | ⏳ | SPEC §4.2「B 阻塞于 A」 | 代码 Task 无此字段 |
| SLA | ⏳ | §9.2 = deadline(真实时间)+priority | `deadline_tick` 未迁移（`task.py`/`intent.py`） |

## C. 何时做 —— 调度层

| 概念 | 状态 | 工作方式 | 缺口 |
|---|---|---|---|
| AgentScheduler | ✅ | `scheduler.py`：事件驱动 ready set + WakeCondition；每 tick 每 agent≤1 激活；claim/defer/consume/requeue(回滚) | ready set 按 `agent_id` 排序，**无 (priority,deadline) 排序**、无容量上限 |
| 激活容量 | ⏳ | §14.1 `max_active_agents_per_tick`（T11 决策 2 已定） | 未实现 |
| Calendar / ScheduleRule | ⏳ | §9.1 cron 子集（日/周；决策 4 已定真实日历） | **代码零实现** |
| WorkerPool | ❓ | §9.3 同质 worker + 路由 | 零实现；**路由语义（决策 3）未定**：立即路由 vs 延迟接单 |

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

**ⓐ 已定未做（欠实现不欠决策，按 roadmap 排队）**：deadline_tick→真实时间
迁移(B)；激活容量(C)；Calendar(C)；WorkerPool 骨架(C)；依赖 depends_on(B)；
ApprovalGate(D)；Human Worker(D)。

**ⓑ 真正设计空白（需决策）**：
1. **WorkerPool 路由语义（T11 决策 3）**：立即路由（委派时按策略选中 worker，
   转普通委派）vs 延迟接单（入池待认领，claim 原子性+悬空兜底）。
   注：OI-005/OI-006 溯源显示原始客服场景要的是**立即路由**（委派到池、按负载/
   技能选中、任务立即有人负责可追踪）；自主认领语义在来源链中未出现。

**ⓒ 潜在偏离/扩张（非缺陷，需显式定位）**：WorkerPool 本身。与 Email/组织架构
核心的边界判断——
- 组织树 / 委派 / Email：**点对点、少而精、可精确指定人**（组织架构建模）；
- WorkerPool：**量大、同质、可互换、不关心是谁**（客服运营建模）。
接 `pool_id` 后 `DelegateIntent.recipient` 从「指定人」扩展为「指定人或指定
供给组」，邮箱投递之外的第二种选址逻辑进入 Schedule/Commit——这是唯一需要在
架构层拍板的分歧，其余缺口都是已定未做的排队。

## 交叉引用

- `KANBAN/TODO/scheduler-calendar-pool`（T11，决策 3 待裁决）
- `KANBAN/TODO/human-worker`（T12a）
- `KANBAN/OPEN_ISSUE/extension-surface-spec`（OI-005/006 出处：Authority/编排层/池）
