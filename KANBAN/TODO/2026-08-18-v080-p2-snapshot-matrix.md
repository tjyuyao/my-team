---
kind: task
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§14；KANBAN/PLAN/v0.8.0-plan（P2-8）
priority: medium
---

# v0.10-16b: v0.8 遗留 — Snapshot 覆盖矩阵（P2-8）

## 目标
为冻结/回滚/持久化的状态面补齐逐行覆盖矩阵测试，收掉 v0.8.0
计划 P2-8。

## 要求 / 规则
- 覆盖 TaskTree / Scheduler claims / Pending ops / Private files
  版本视图 / Shared KB / 外部进程 / LLM 请求 / ID 分配 /
  state_epoch，逐行验证 Freeze 可见性 / Commit 可回滚性 / 持久化。

## 产出
- Snapshot 矩阵测试文件。

## 验收标准
- [ ] Snapshot 矩阵全部行通过（测试可见）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
