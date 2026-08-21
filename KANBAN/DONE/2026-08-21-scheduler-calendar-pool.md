---
kind: task
status: completed
phase: v0.10 调度
source: SPEC §9；OI-005 §1.1、OI-006 §3
priority: high
---

# v0.10-11: Calendar Scheduler、SLA 优先级与 WorkerPool 路由


## 目标
支持周期性任务（每日发布/每周直播）、按 SLA 优先级调度、以及
委派到 WorkerPool 而非指定 Agent。

## 要求 / 规则
- `ScheduleRule`：`{rule_id, target, cron | interval_ticks,
  next_run_tick, action}`；每 tick 评估，到期生成 TIMER_EXPIRY
  事件或创建任务。
- Task 携带 `deadline`（真实时间，SPEC §9.1 时间模型）+ `priority`；
  Schedule 阶段就绪集按 `(priority, deadline)` 排序（真实时间直接比较，
  业务层无 tick 概念）。
- 到期前 N tick 生成 `DEADLINE_APPROACHING` 事件；超时走结构化
  escalation（on/mode/target，见 Authority 方向），不硬编码
  「通知 Manager → 转人工 → 关闭」阶梯。
- `WorkerPool` = 一个 `kind=service` manager + children + 声明式路由
  规则（round_robin / least_busy / skill_match）；无独立 `pool_id`
  机制，`DelegateIntent.recipient` 仍为 `agent_id`（指向该 manager），
  池路由行为由 manager 内部按 `routing`（immediate|deferred）执行，
  结果写入 Journal。

## 产出
- Calendar Scheduler 与 WorkerPool 路由。
- SLA 排序与升级事件。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **回滚交互（最深）**：`ScheduleRule.next_run_tick` 推进必须在 commit 时
  生效，否则 tick 回滚后 TIMER_EXPIRY 丢失或重触发；规则推进是新增持久
  状态，须纳入 T18 逆操作契约（invert）语义（scheduler 已有
  `requeue_events` 回滚恢复，但"规则推进"是新增面）。
- **就绪集排序**：现状 `AgentScheduler.compute_ready_set` 按 `agent_id`
  确定性排序，无 priority/deadline 概念（Task 模型已有 priority +
  `deadline_tick` 遗留字段，迁移见决策进展）；需与「每 tick 一轮唯一
  执行」并发约束交互设计。
- **WorkerPool 接单竞态（已被决策 3 消解）**：原担心 `recipient` 扩为
  `agent_id | pool_id` 后池内谁接单/防双接需原子语义；决策 3 定为
  pool = service manager 后，分配权在 manager 单点串行、同 tick 提交原子，
  child 不并行抢单，竞态不存在。委派语义见任务模型定稿（§4.2：
  委派=建副本，assigner/assignee 责任声明）。
- **cron vs tick**：cron 表达式与模拟时间的映射是决策点（interval_ticks
  简单，cron 需定对齐规则）。
- **先决设计问题（开工时定）**：① cron 与模拟时间映射；② 就绪集排序与
  每 tick 一轮的交互；③ manager 委派副本给 child 的 **tick 时序**（委派
  副本在 manager 所在 tick 立即可见、还是 child 下 tick 才激活；责任由
  assigner/assignee 声明，不再悬而未决）。

### 先决问题裁决（2026-08-21 开工定）

- **① cron 与时间映射——已定：TickEngine 增加虚拟墙钟**。
  `wall_now = anchor + current_tick × tick_duration`（anchor 为真实
  datetime 锚点，可注入；确定性、可回放）。业务字段（Task/Email/
  DelegateIntent 的 `deadline`、cron 触发时刻）一律真实 datetime；
  每 tick 用 `wall_now` 直接比较。cron 子集用结构化表达：
  `{freq: daily|weekly, days_of_week: [0-6], at_time: "HH:MM"}`（日/周
  维度），不做字符串 cron 解析。`interval_ticks` 规则保留为引擎刻度节奏
  （非业务时间）。**引擎侧 op 超时（pending_ops/tool_protocol 的
  deadline_tick）属 tick 域，不改名不迁移**。
- **② 就绪集排序与容量交互——已定**：`compute_ready_set` 按 agent 最紧急
  任务排序（priority 降序 → deadline 升序[真实时间] → agent_id 兜底），
  无任务者排最后；`ExecutionConfig.max_active_agents_per_tick`（默认 8）
  截取容量内激活；超容候选的事件 requeue 回 QUEUED（QUEUED 存活 end_tick，
  下 tick 重转 ELIGIBLE 再竞争——幂等无状态损失）。
- **③ manager 委派副本 tick 时序——已定**：
  - *立即模式*：上游 DelegateIntent→pool manager 在同 tick Act/Commit
    展开为一组原子 effect：原任务(assignee=manager) + 副本任务
    (assigner=manager, assignee=选中 child, derived_from=原任务) + 委派
    邮件；child 下 tick 经 wake event 激活。
  - *延迟模式*：原任务(assignee=manager)提交 → Publish 发
    DELEGATION_RECEIVED 唤醒 manager → manager（kind=service，规则驱动
    Decide，无 LLM 调用）下 tick 激活，从待分配区（可无状态推导：owner=
    manager 且 ASSIGNED 且无 derived 副本的任务）按策略选 idle child 发
    副本 DelegateIntent；child 完成/取消任务时 Publish 发 CHILD_IDLE
    唤醒 manager 续派。
  - 责任链全部由 assigner/assignee 字段声明；creator/owner→
    assigner/assignee 的代码改名随本卡落地（或立后续卡，视迁移面）。

## 决策进展（2026-08-19，讨论定稿）

- **决策 4（日历）——已定：引入真实日历**。业务语义一律真实时间：
  deadline / cron 触发时刻直接挂真实日历；tick 仅为底层引擎的离散时间
  （推进机制，每 tick 检查真实时钟、处理到期），对业务层完全透明、不可感知。cron
  子集（日/周维度）。理由：对接外部业务（每日发布/每周直播本质是日历
  语义）；"固定 tick 偏移"曾被视为替代，系目标裁剪，已否决。
- **实现注记（字段迁移）**：现有 Task/Email 模型的 `deadline_tick` 字段为
  早期实现遗留（tick 化存储），T11 落地时迁移为真实时间 `deadline`，业务
  层不再出现 tick 字段。
- **决策 2（SLA 排序）——已定：引入激活容量** `max_active_agents_per_tick`。
  Schedule 阶段按 `(priority, deadline)`（真实时间直接比较）决定容量内
  激活，超容者保持
  就绪、下 tick 再竞争（幂等、无状态损失）；排序键取 agent 最紧急任务。
  系统抗超负荷能力总览见 SPEC §14。
- **决策 3（池路由）——已定：不设独立 WorkerPool 原语；pool = 一个
  `kind=service` manager + children + 声明式路由规则**。选择动作本就存在
  （LLM manager 复杂判定选人，service manager 读状态+规则选人是同一动作的
  规则最简退化形），pool 只是这一动作的退化档，不值得独立设计。
  立即/延迟是同一 manager 的**两种可配置行为**（`routing` 配置项切换），
  均不引入框架新实体：
  - **立即（指派式）**：manager 收到委派当场按规则选中 child 并**委派副本**
    （新 task，assignee=该 child）——零唤醒，复用"委派→直接子级"现成路径；
    等待期任务已归属目标 child。
  - **延迟（认领式）**：任务先入 manager 的**待分配区（manager 自身状态
    字段，非新实体）**，等观察到某 child 空闲/有容量再转——多一个
    "child 空出→唤醒 manager 再分派"的 WakeEvent hook。
  - **无认领竞态**：分配权在 manager（单点、同 tick 提交原子性），child
    不并行抢单，"两个空闲同时抢一单"在这一架构下不存在。
  由此 `DelegateIntent.recipient` 不需扩展 `pool_id`（委派目标就是那个
  service manager）；Pool 专有机制（`AgentConfig.worker_pools`、
  `owner_pool`、pool_id 路由逻辑）全部删除。前提：`kind=service`（SPEC
  §4.1 已规划、代码未实现）需先落地。
- **任务模型（2026-08-19 定稿，SPEC §4.2）**：任务书**不可变**；责任由
  `assigner`/`assignee` 声明（**不限 kind**——规则/LLM 均可为责任人）；
  向下委派即使一字不改也**新建副本**（新 task_id），责任随副本转移，形成
  逐层独立的责任关系；任务树**不单独持久化**，由副本间 `derived_from`
  引用沿委派链动态推导（组织/审计视图）。
- **术语**：SLA 全称与定义已补入 SPEC §9.2（Service Level Agreement，
  服务等级协议 = deadline（真实时间）+ priority 承载的外部业务承诺）。
  业务层一律真实时间，无 tick 概念（§9.1）。

## 验收标准
- [x] interval 规则每 N tick 生成一次事件/任务
  （`test_calendar_scheduler.py::test_interval_rule_creates_task_every_n_ticks`）
- [x] 就绪集按 priority/deadline 排序（测试可断言顺序）
  （`test_sla_capacity.py`，含容量截取与超容再竞争）
- [x] deadline 前 N tick 生成 DEADLINE_APPROACHING
  （`test_deadline_monitor.py`，真实时间阈值 + 回滚 un-mark 防丢唤醒）
- [x] 委派到 pool 后任务由池内 Worker 接单，且遵守路由策略
  （`test_pool_routing.py`：least_busy / round_robin / skill_match +
  立即展开副本 derived_from + 延迟分派排队）
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过（905 passed）

## 实现注记（2026-08-21 落地）

- **提交切面**：A 时间地基+deadline 迁移 → B SLA 排序+容量 → C deadline
  监控 → D Calendar → E Pool。引擎侧 op 超时（pending_ops/tool_protocol
  的 `deadline_tick`）属 tick 域，保留不改名。
- **延迟分派实现细化**：未用"child 空出→WakeEvent 唤醒 manager"钩子，
  而是 Ingest 每 tick 无状态推导待分配（owner=manager ∧ ASSIGNED ∧ 无
  derived 副本）×空闲 child 并原子暂存——语义等价（分派都发生在容量释放
  的下一 tick），少一个唤醒钩子；一人公司规模下轮询成本可忽略。
- **round_robin 游标为内存态**：重启归零（仅影响公平性，不影响正确性；
  默认策略 least_busy 无状态）。
- **escalation 归一为特殊邮件**（含责任转移位）按用户设计属 v0.11 E1
  process-model；本卡交付的"升级事件"即 DEADLINE_APPROACHING /
  TIMER_EXPIRY 结构化唤醒。
- **creator/owner→assigner/assignee 改名未随本卡落地**：Task 模型已带
  `derived_from` 与真实时间 `deadline`，字段改名是纯机械迁移，留作独立
  小卡避免本卡 diff 失控。
