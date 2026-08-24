---
kind: task
phase: v0.10 人类参与
source: SPEC §10.1；OI-005 §3、OI-006 §3
priority: high
status: completed
---

# v0.10-12a: Human Worker（kind=human Agent）

## 范围注记（2026-08-18 重划后）
本卡只管 Human Worker（kind=human 的 Agent 承接任务），不含审批
（ApprovalGate 已并入 v0.11 E1 HumanTask / E2 三查分离）。

## 目标
人类可以作为组织树中的 Worker 接受委派与任务，动作走与 AI Worker
相同的事务路径。

## 要求 / 规则
- `AgentConfig.kind="human"`：Human Worker 有独立任务队列，Manager
  可像对 AI Worker 一样委派。
- 人类通过 UI `accept / complete / fail`，动作翻译为 Intent 走相同
  事务路径（与 AI Worker 一致，不另起通道）。
- 人类任务有 deadline 与结构化 escalation（on/mode/target，不硬编码
  「通知 Manager → 转人工 → 关闭」）。
- **身份注记**：本卡最小实现以 org-tree 的 `agent_id` 为身份，不伪造
  `from/to` 字段；完整 Identity 闭包（内核注入、不可伪造）依赖后续
  落地（SPEC §12.1，见 v0.11 P1 backlog）。

## 产出
- Human Worker 最小闭环（Manager 委派 → 人完成）。
- 人类任务队列与 UI 动作 → Intent 的翻译路径。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **T9 已铺路（勿重复发明）**：人类真实时间工作 vs 内核 tick 的鸿沟由
  `WAITING_FOR_EXTERNAL` 承接——委派后 Agent 挂起，人的完成动作作为
  IngressEvent（human-action 事件源）注入，走 `_consume_ingress` 回执/
  唤醒通道。
- **入站动作翻译**：accept/complete/fail 须翻译为 Intent 走与 AI Worker
  相同事务路径；UI 动作非 LLM 生成，authority/身份如何挂（最小实现以
  org-tree 的 agent_id 为身份，不伪造 from/to，见范围注记）。
- **late-result 竞态**：人完成时任务可能已超时 escalated——参考 T9
  pending_ops 的 late-result 语义（不能复活已完结任务）。
- **首个真实消费者**：本卡是 WAITING_FOR_EXTERNAL / Ingress 的第一个真实
  消费者，开工前先验证 wait/wake 对外部主动注入路径完备。

## 实现注记（2026-08-23 完成）
- **运行时**：`HumanWorkerRuntime`（agent_runtime.py）——kind=human 专用，
  空工具面（UI 驱动，无 LLM/工具路径）；`decide_intents` 只把 observation
  的 `pending_human_actions` 翻译为 AcceptTaskIntent / CompleteTaskIntent /
  FailTaskIntent，走标准事务路径。`_create_runtime` 按 kind 分支。
- **命令面**：`human_control.submit_task_action/accept_task/complete_task/
  fail_task`——校验（任务存在、assignee 是 kind=human、非终态）后构造
  `IngressEvent(source="human")` 注入 IngressBuffer（dedup 键
  `task_id:action`，重复点击幂等）；`execute()` 路由已挂三个命令。
- **Ingest 路由**：`_consume_ingress` 对 `source=="human"` 事件定向路由到
  任务 assignee（kind=human），写入 `_pending_human_actions` 并 enqueue
  `HUMAN_ACTION` wake（visible 同 tick）；不可路由（动作非法/任务缺失/
  assignee 非 human）审计后丢弃，不落入 advisory 广播。消费动作记录于
  `_human_actions_consumed_this_tick`，tick 回滚时恢复（不丢动作）。
- **持久化**：`human_pending_actions` 组件进 snapshot/restore（崩溃于
  Ingest drain 与 Decide 翻译之间不丢动作）。
- **结构化 escalation**：Publish 阶段（post-commit）对本次过期的
  kind=human 任务发结构化升级邮件给 assigner（on=unresolved /
  mode=advise / target=assigner）+ 审计；回滚 tick 不 escalate。完整
  escalation 机制（特殊邮件 + 责任转移位）仍归 v0.11 E1（T11 注记）。
- **AcceptTaskIntent**：新 intent 类型（models/intent.py），Act 阶段
  stage TASK_UPDATE(status=accepted)。TASK_UPDATE apply 顺带持久化
  `reason`（fail 路径，AI worker 同步受益）。
- **身份**：最小实现以命令发起方为 operator（human.user_001），任务
  归属由 assignee=kind=human agent 判定；不伪造 from/to（范围注记）。

## 验收标准
- [x] 人类 Worker 可被 Manager 委派并完成任务（走事务路径）
- [x] `kind=human` 有独立任务队列；`accept/complete/fail` 均翻译为 Intent
- [x] 人类任务超时产生结构化 escalation（而非硬编码阶梯）
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过（934 passed，ruff/mypy clean，kanban_lint 0）
