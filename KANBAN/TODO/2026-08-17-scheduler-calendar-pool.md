# v0.10-11: Calendar Scheduler、SLA 优先级与 WorkerPool 路由

**Phase:** v0.10 调度
**Source:** SPEC §9；OI-005 §1.1、OI-006 §3
**Priority:** high

## 目标
支持周期性任务（每日发布/每周直播）、按 SLA 优先级调度、以及
委派到 WorkerPool 而非指定 Agent。

## 要求 / 规则
- `ScheduleRule`：`{rule_id, target, cron | interval_ticks,
  next_run_tick, action}`；每 tick 评估，到期生成 TIMER_EXPIRY
  事件或创建任务。
- Task 携带 `deadline_tick + priority`；Schedule 阶段就绪集按
  `(priority, deadline_tick)` 排序。
- 到期前 N tick 生成 `DEADLINE_APPROACHING` 事件；超时结构化
  升级（通知 Manager → 转人工 → 关闭）。
- `WorkerPool`：一组同质 Worker + 路由策略
  （round_robin / least_busy / skill_match）。
- `DelegateIntent.recipient` 支持 `agent_id` 或 `pool_id`；
  池路由结果写入 Journal。

## 产出
- Calendar Scheduler 与 WorkerPool 路由。
- SLA 排序与升级事件。

## 验收标准
- [ ] interval 规则每 N tick 生成一次事件/任务
- [ ] 就绪集按 priority/deadline 排序（测试可断言顺序）
- [ ] deadline 前 N tick 生成 DEADLINE_APPROACHING
- [ ] 委派到 pool 后任务由池内 Worker 接单，且遵守路由策略
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
