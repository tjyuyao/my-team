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

## 验收标准
- [ ] 人类 Worker 可被 Manager 委派并完成任务（走事务路径）
- [ ] `kind=human` 有独立任务队列；`accept/complete/fail` 均翻译为 Intent
- [ ] 人类任务超时产生结构化 escalation（而非硬编码阶梯）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
