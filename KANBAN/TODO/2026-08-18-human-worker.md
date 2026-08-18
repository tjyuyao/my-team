---
kind: task
phase: v0.10 人类参与
source: SPEC §10.1；OI-005 §3、OI-006 §3
priority: high
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

## 验收标准
- [ ] 人类 Worker 可被 Manager 委派并完成任务（走事务路径）
- [ ] `kind=human` 有独立任务队列；`accept/complete/fail` 均翻译为 Intent
- [ ] 人类任务超时产生结构化 escalation（而非硬编码阶梯）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
