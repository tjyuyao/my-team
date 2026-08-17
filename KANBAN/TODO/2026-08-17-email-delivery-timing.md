# Email Delivery Timing Semantics

**Phase:** 2 - Email Collaboration
**Source:** SPEC §13.3; review #16; report §7 P2
**Priority:** P2 — Semantic Completeness

## 目标

正式定义邮件投递的时间边界语义。

## 背景

当前行为：邮件创建时 `deliver_at_tick = t + 1`。但以下语义未正式定义：

- `deliver_at_tick == t`（同 tick 投递）是否允许？
- `deliver_at_tick < created_at_tick` 时如何处理？
- tick `t` 的 Act 阶段创建的邮件能否在 tick `t` 的 Observe 阶段可见？
- Pause 期间创建的邮件何时投递？

## 要求

1. 形式化规则："tick `t` 的 Act/Commit 阶段生成的邮件，最早在 tick `t+1` 的 Deliver 阶段投递"
2. `deliver_at_tick < current_tick` 时自动修正为 `current_tick + 1`
3. 禁止 `deliver_at_tick == current_tick`（同 tick 不投递）
4. 添加边界测试覆盖上述场景
5. 添加 `test_email_same_tick_semantics.py`

## 产出

- [ ] 修改 `mailbox.py` 投递逻辑
- [ ] 添加 `test_email_same_tick_semantics.py`
- [ ] 更新文档说明投递时序

## 验收标准

- [ ] tick `t` 发送的邮件在 tick `t+1` Deliver 阶段才投递
- [ ] `deliver_at_tick < current_tick` 被修正
- [ ] 同 tick 发送和接收的边界测试通过
