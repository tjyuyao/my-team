---
kind: task
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
- Task 携带 `deadline_tick + priority`；Schedule 阶段就绪集按
  `(priority, deadline_tick)` 排序。
- 到期前 N tick 生成 `DEADLINE_APPROACHING` 事件；超时走结构化
  escalation（on/mode/target，见 Authority 方向），不硬编码
  「通知 Manager → 转人工 → 关闭」阶梯。
- `WorkerPool`：一组同质 Worker + 路由策略
  （round_robin / least_busy / skill_match）。
- `DelegateIntent.recipient` 支持 `agent_id` 或 `pool_id`；
  池路由结果写入 Journal。

## 产出
- Calendar Scheduler 与 WorkerPool 路由。
- SLA 排序与升级事件。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **回滚交互（最深）**：`ScheduleRule.next_run_tick` 推进必须在 commit 时
  生效，否则 tick 回滚后 TIMER_EXPIRY 丢失或重触发；规则推进是新增持久
  状态，须纳入 T18 逆操作契约（invert）语义（scheduler 已有
  `requeue_events` 回滚恢复，但"规则推进"是新增面）。
- **就绪集排序**：现状 `AgentScheduler.compute_ready_set` 按 `agent_id`
  确定性排序，无 priority/deadline 概念（Task 模型已有两字段，数据层
  现成）；需与「每 tick 一轮唯一执行」并发约束交互设计。
- **WorkerPool 接单竞态**：`DelegateIntent.recipient` 从 `agent_id` 扩为
  `agent_id | pool_id`；池内谁接单、同一任务防双接，需原子语义；路由
  结果写 Journal。
- **cron vs tick**：cron 表达式与模拟时间的映射是决策点（interval_ticks
  简单，cron 需定对齐规则）。
- **先决设计问题（开工时定）**：① cron 与模拟时间映射；② 就绪集排序与
  每 tick 一轮的交互。

## 决策进展（2026-08-19，讨论定稿）

- **决策 4（日历）——已定：引入真实日历**。tick ↔ 日映射
  （`day_length_ticks` 可配置）+ cron 子集（日/周维度）。理由：对接外部
  业务（每日发布/每周直播本质是日历语义）；"固定 tick 偏移"曾被视为替代，
  系目标裁剪，已否决。
- **决策 2（SLA 排序）——已定：引入激活容量** `max_active_agents_per_tick`。
  Schedule 阶段按 `(priority, deadline_tick)` 决定容量内激活，超容者保持
  就绪、下 tick 再竞争（幂等、无状态损失）；排序键取 agent 最紧急任务。
  系统抗超负荷能力总览见 SPEC §14。
- **决策 3（池路由）——待定**：立即路由（委派时按策略选中 worker，无竞态）
  vs 延迟接单（任务入池待认领，有 claim 原子性 + 悬空兜底成本）。等裁决。
- **术语**：SLA 全称与定义已补入 SPEC §9.2（Service Level Agreement，
  服务等级协议 = deadline_tick + priority 承载的外部业务承诺）。

## 验收标准
- [ ] interval 规则每 N tick 生成一次事件/任务
- [ ] 就绪集按 priority/deadline 排序（测试可断言顺序）
- [ ] deadline 前 N tick 生成 DEADLINE_APPROACHING
- [ ] 委派到 pool 后任务由池内 Worker 接单，且遵守路由策略
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
