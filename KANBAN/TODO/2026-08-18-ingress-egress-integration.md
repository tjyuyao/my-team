---
kind: task
phase: v0.10 边界
source: SPEC §8.1、§8.2；OI-005 §3、OI-006 §3
priority: high
---

# v0.10-9: Ingress/Egress 传输层与 Integration 注册（映射并入 v0.11 E1）

## 范围注记（2026-08-18 重划后）
本卡收敛为**方向中立的传输层**：可靠入站、去重、ack、Integration 注册、
出站 pending op。事件入内核后的**映射前门**（`IngressEvent →
ProcessInstance`）属 v0.11 编排层 E1，本卡不再包含，也不按旧设计
（直接转 WakeEvent/TaskCreate/Record/Email）开工。见 SPEC §8.1。

## 目标
外部平台事件（消息、评价、订单、评论、数据回传）能可靠进入内核；
出站请求统一走 pending op；平台适配器作为 Integration 一等公民注册运行。

## 要求 / 规则
- `IngressEvent` 模型：source、external_id、event_type、occurred_at、
  payload、idempotency_key、priority、deadline_hint。
- `IngressBuffer`：tick 之间写入，Ingest 阶段消费；`(source, external_id)`
  持久化去重；事件持久化成功后才 ack。
- Ingest 阶段可唤醒相关 Agent（"有事件到达"），但不隐式决定下游对象；
  下游（流程实例化）由 v0.11 E1 的 `IngressEvent → ProcessInstance` 接管。
- `Integration` 注册：name、credential_ref、rate_limits、manifests
  （出站工具）、ingress_event_types、health_check。
- 出站工具（EXTERNAL_IRREVERSIBLE）必须提供幂等键与状态回查；平台限流
  由 Admission 强制执行。
- 先用假平台适配器（脚本/webhook 模拟器）做集成测试。

## 产出
- IngressBuffer 与 Integration 注册中心。
- 出站 pending op（幂等键 + 状态回查）。
- 一个假平台适配器 + 传输层集成测试。

## 验收标准
- [ ] 平台事件注入后被可靠持久化，下一 tick 唤醒相关 Agent（仅"事件到达"
  通知，不隐式创建任务）
- [ ] 重复 `(source, external_id)` 跨重启只入站一次
- [ ] 出站工具在限流时保持 SUBMITTED 背压
- [ ] 事件未持久化前不 ack（可测试故障注入）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
