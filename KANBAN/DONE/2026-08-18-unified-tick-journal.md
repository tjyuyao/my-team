# v0.9-4: 统一 TickJournal（单一事实源）

**Phase:** v0.9 基础
**Source:** SPEC §3.2；OI-004 §2.2
**Priority:** high
**Completed:** 2026-08-18
**Tests:** 694 passed（+18），ruff clean，mypy clean（38 source files）

## 目标
引入 append-only `TickJournal` 作为所有状态变更的唯一事实源；
PendingOps、Outbox、Audit、RecordStore、KPI 都是 Journal 的投影。
回滚/恢复/审计不再依赖多账本手工同步。

## 要求 / 规则
- `TickRecord` 包含：tick、epoch、快照哈希、intents、验证结果、
  effects（含最终状态）、pending op 注册/取消、outbox 条目、
  审批请求、审计事件。
- Commit 成功 → Journal commit；回滚 → Journal 标记 aborted。
- `AuditLog` 改为从 Journal 投影生成（或至少审计写入与 Journal
  原子化，不再独立追加）。
- 持久化以 Journal 为源；`save_to/load_from` 能从 Journal 重放。
- 先做内存版 Journal，SQLite 持久化随后（v0.8 已有持久化框架）。

## 渐进式第一阶段约束（用户确认 2026-08-18）

1. **TickJournal 模型先行**：TickRecord + TickJournal 类；Journal 本身
   进入 `_collect_state/_restore_state`，否则跨重启后 AuditLog 投影会
   与 Journal 脱节。
2. **AuditLog 投影但保留接口**：所有 `_audit_log.record(...)` 调用点
   **保持不变**，内部委托写入当前 TickRecord；AuditLog 只读投影从
   Journal 生成。这样不动几十个调用点，又消灭"审计独立账本"。
3. **`_phase_commit` 写入 TickRecord**：成功→记录 committed effects +
   pending op 注册 + outbox 条目；回滚→记录 aborted + rolled-back effects。
4. **PendingOps / Outbox 暂不改投影**：它们继续以现有 registry 为准；
   TickRecord 记录本 tick 对它们的变更，给后续"完全重放"留好数据。

## 产出
- `TickJournal` 模型与投影接口。
- 迁移 `_phase_commit` 与回滚路径到 Journal。
- 测试：投影一致性、回滚 aborted、跨重启重放。

## 验收标准
- [ ] 所有状态变更可写进 TickRecord
- [ ] 审计事件可由 Journal 投影生成（不独立追加）
- [ ] 回滚 tick 在 Journal 中标记 aborted 且投影一致
- [ ] `save_to/load_from` 跨重启后 AuditLog 与 Journal 一致
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
