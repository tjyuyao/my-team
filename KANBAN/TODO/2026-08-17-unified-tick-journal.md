# v0.9-4: 统一 TickJournal（单一事实源）

**Phase:** v0.9 基础
**Source:** SPEC §3.2；OI-004 §2.2
**Priority:** high

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

## 产出
- `TickJournal` 模型与投影接口。
- 迁移 `_phase_commit` 与回滚路径到 Journal。
- 测试：投影一致性、回滚 aborted、跨重启重放。

## 验收标准
- [ ] 所有状态变更可从 Journal 重放
- [ ] 审计事件可由 Journal 投影生成（无需独立记录入口）
- [ ] 回滚 tick 在 Journal 中标记 aborted 且投影一致
- [ ] `save_to/load_from` 跨重启从 Journal 恢复
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
