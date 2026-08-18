---
kind: task
status: completed
phase: v0.9 收口
source: SPEC §3、§14；OI-003 P1-6/P1-7/P2-8
priority: medium
---

# v0.9-15: 一致性与可观察性收口（版本号、事件可见性、workspace_version）

**Completed:** 2026-08-18
**Tests:** 676 passed（+7），ruff clean，mypy clean（37 source files）

## 目标
仓库内版本号、阶段模型、事件可见性语义、快照版本使用保持一致，
可观察性字段与真实内核一致。

## 要求 / 规则
- 统一版本号：`src/my_team/__init__.py.__version__` 与
  `pyproject.toml version` 一致，从 v0.9.0 起单一权威来源。
- 阶段模型：SPEC §3.1 与代码一致；`last_tick_phases` 与返回的
  TickResult 一致（与 P0-3 联动）。
- 事件可见性：WakeupEvent 增加 `visible_at_tick` 字段；
  `compute_ready_set` 用该字段判断，而不是依赖入队顺序。
- `workspace_version`：SUBMITTED op 保存提交时的冻结视图（或内容
  哈希）；dispatch 校验并使用提交视图，不一致时声明陈旧失败。

## 产出
- 版本号单一来源。
- 事件可见性显式字段。
- 排队 op 的冻结视图绑定。

## 验收标准
- [x] `__version__` 与 pyproject 一致（均为 "0.9.0"）
- [x] 事件只在 `visible_at_tick` 后参与匹配（显式字段，_matches 用它）
- [x] 排队 op 在 workspace 变更后不会读到更新于提交后的文件（_submission_view 绑定）
- [x] 新增 test_consistency_cleanup（+7 tests）；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
