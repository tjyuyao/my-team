# v0.10-9: Ingress/Egress 与外部平台 Integration

**Phase:** v0.10 边界
**Source:** SPEC §8、§6.4；OI-005 §3、OI-006 §3
**Priority:** high

## 目标
外部平台事件（消息、评价、订单、评论、数据回传）能可靠进入内核；
出站请求统一走 pending op；平台适配器作为 Integration 一等公民
注册与运行。

## 要求 / 规则
- `IngressEvent` 模型：source、external_id、event_type、
  occurred_at、payload、idempotency_key、priority、deadline_hint。
- `IngressBuffer`：tick 之间写入，Ingest 阶段消费；
  `(source, external_id)` 持久化去重；事件持久化成功后才 ack。
- `IngressEvent` 可转换为 WakeEvent / TaskCreate / Record / Email，
  由场景包配置映射。
- `Integration` 注册：name、credential_ref、rate_limits、
  manifests（出站工具）、ingress_event_types、health_check。
- 出站工具（EXTERNAL_IRREVERSIBLE）必须提供幂等键与状态回查；
  平台限流由 Admission 强制执行。
- 先用假平台适配器（脚本/webhook 模拟器）做集成测试。

## 产出
- IngressBuffer 与 Integration 注册中心。
- 一个假平台适配器 + 集成测试。

## 验收标准
- [ ] 平台事件注入后，Agent 在下一 tick 被唤醒并创建任务
- [ ] 重复 `(source, external_id)` 跨重启只入站一次
- [ ] 出站工具在限流时保持 SUBMITTED 背压
- [ ] 事件未持久化前不 ack（可测试故障注入）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
