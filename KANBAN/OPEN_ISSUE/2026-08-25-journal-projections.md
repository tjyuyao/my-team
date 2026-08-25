---
kind: issue
status: open
source: grill-me 会话（2026-08-25）；SPEC §1.6/§3.2/§5.9
priority: medium
---

# Journal 派生视图（投影）暂缓实现

**Opened:** 2026-08-25
**Status:** OPEN — 暂缓，非 v0.11 范围

## 决定

Journal 本体保留：**append-only 账本 + 单一事实源**（TickRecord 记录
Intents/effects/pending op/outbox/审批/审计事件，Commit 提交、rollback
标记 aborted，SPEC §3.2）。

以下**派生视图（projection）暂缓/裁撤**——即「一切状态从 Journal 重放
推导」的统一投影层，现在不做：

| 派生视图 | 含义 | 现状态 |
|---|---|---|
| 审计 AuditLog | 从 Journal 派生的可读审计视图 | 现为独立组件（audit.py），非 Journal 投影 |
| 对账 Reconciliation | 与外部平台（淘宝/微信/git）状态核验 | **裁撤**（2026-08-25：随 N6 一起，见 DONE pending-outbox-recovery） |
| 重放 Replay | 给定历史重建任意时刻状态 | 已降级（记录而非重放） |
| 恢复 Recovery | 崩溃后从 committed tick 恢复 | **裁撤**（2026-08-25：随 N6 一起；崩溃恢复机制从未实现） |
| KPI | 指标看板 | 独立视图，暂缓投影化 |

## 理由（来自 grill）

「重放」听起来非常重；现实世界「不能两次踏入同一条河流」。My-Team 的
原则是**记录（账本）而非重放（时间机）**——append-only Journal 便宜、
简单、天然；派生视图**谁需要谁投影**，但**现在不实现统一投影层**。
安全 = 简单原则 + 可扩展审计，不做完备证明。

## 影响

- SPEC §1.6/§3.2/§3.6/§5.9 已撤掉「都是 Journal 投影」的表述，改为
  「派生视图暂缓」。
- **恢复与对账机制裁撤**（2026-08-25，随 N6）：崩溃恢复、unknown
  状态机、补偿/对账不做；outbox 幂等键稳定化记入 P1 backlog。
  原则 = 日志可复盘，不做时间机。
- RecordStore「删 ledger、重放源唯一 = Journal」的迁移方向随投影暂缓
  一并后移；RecordStore 现持当前状态即可。

## 待议

- [ ] 何时需要第一个派生视图（最可能是「审计」），以什么触发条件
      重新启动投影层设计。
