---
kind: task
phase: v0.11 post-agent
source: 原 E3；SPEC §3.2/§8.2/§6.4；三态收敛（2026-08-24）
priority: high
---

# 恢复与对账：pending op 生命周期 + outbox 恢复 + unknown/对账


## 目标
闭合三处可靠性缺口：①"不得跨 tick 孤儿 op"与"异步 op 天然跨 tick"
的矛盾；②Commit 成功 → Publish 前崩溃的故障窗口（Journal 已记录
但外部调用未发送/重复发送）；③外部不可逆操作崩溃后误判为失败
（请求可能已在平台侧生效）。

> **注记（2026-08-25）**：「统一 Journal 投影化」（把 pending/outbox/
> 审计/KPI 全部变为 Journal 重放派生）**暂缓**，见 OPEN_ISSUE
> journal-projections。本卡保留运行正确性部分：pending op 七绑定 +
> outbox 恢复 + 稳定幂等键 + `unknown` 不自动重复 + 外部不可逆的
> 补偿/对账。**「对账」作为完整 Journal 投影层后移**，不影响本卡的
> 崩溃恢复与幂等交付。

## 要求 / 规则
- **改措辞**：禁止的是"无归属、无生命周期、无恢复语义的孤儿 op"，
  不是"跨 tick"。`PendingOperation` 可跨 tick，但必须七绑定：owner
  Deployment / ProcessInstance 或 AgentContinuation / 创建
  package-version / 明确状态机 / deadline-retry-cancel-recovery /
  Journal 可追踪 / 重启可恢复或人工处置（三态收敛后：owner 可为
  position 或 agent，挂接 N2）；
- `pending_ops.py` 状态机补 `unknown / stale / compensation_required`
  （现有 submitted/pending/completed/failed/cancelled/timed_out 之上）；
- `outbox.py` 状态机补为持久化发布日志：
  `COMMITTED → OUTBOX_READY → DISPATCHING → SENT → ACKED →
  CONFIRMED`（outbox 数据归邮箱设备，N1 联测）；
- **修复 `_make_key` 的 uuid4 不稳定性**（outbox.py:209 仍为随机
  后缀）：幂等键必须是稳定键（跨重启一致），否则崩溃恢复重复外部
  操作；
- 恢复流程：从世界记忆设备（Journal）找 `COMMITTED` 未完成 outbox
  → 稳定幂等键重试 → 支持状态回查的先回查再重试 → 无法确认进入
  `unknown`（不自动重复）→ 人工对账入口；
- 明确区分（写入 spec）：内核事务原子性（tick 回滚只回滚内存
  effect）/ 外部效果最终一致性（tick 回滚**不回滚平台写入**）/
  补偿事务 / 对账状态。

## 产出
- pending op 完整生命周期 spec + outbox recovery 状态机 spec；
- `pending_ops.py`/`outbox.py` 扩展（unknown 状态、稳定幂等键、
  恢复入口、对账接口）；
- 崩溃恢复测试：Commit 成功但 Publish 前崩溃 → 重启可恢复 outbox
  且幂等。

## 验收标准
- [ ] pending op 可跨 tick，七绑定齐全，无孤儿
- [ ] 崩溃重启后 COMMITTED 未完成 outbox 可恢复且幂等（无重复
      外部调用）
- [ ] `unknown` 状态不自动重复执行不可逆操作
- [ ] 外部事件重复投递不创建重复 Task/意图（除非显式声明允许）
- [ ] tick 回滚不回滚平台写入（边界明确，有测试证明）
- [ ] 最小测试向量段：external irreversible op → crash → recovery →
      compensation / reconciliation 通过
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
