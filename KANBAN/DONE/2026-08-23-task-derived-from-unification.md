---
kind: task
status: completed
phase: v0.10 任务模型
source: SPEC §4.2；KANBAN/DONE/task-assigner-assignee-rename 遗留项
priority: medium
---

# Task 引用字段统一：parent_task_id 并入 derived_from

**Status:** DONE
**Completed:** 2026-08-23

## 背景（2026-08-23 独立立卡）
责任字段改名（creator/owner→assigner/assignee）已由并行工作完成。
本卡承接 SPEC §4.2 术语对齐注记的第二条合并：`parent_task_id` 并入
`derived_from` 引用链。目标模型中委派/分解只有一种边类型（"通过
'分解'这种'任务间引用'关系实际建立任务树"）；现双字段是实现过渡态
——pool 副本对 original 已双写同值，普通委派只填 parent、不填
derived_from。

## 要求 / 规则
- **字段统一**：删除 `Task.parent_task_id`；所有委派路径一律填充
  `derived_from`（值原样保留，零行为变更）。
- `DelegateIntent.parent_task_id` → `derived_from` 同步改名（仅内部
  暴露，不进提示词/context，已实测确认）。
- `TaskTree._parent_map`/`_children_map` 索引机制照旧维护，仅换键名
  来源；cancel 级联 / subtree / ancestors 行为不变。
- **持久化 schema（查证发现）**：`TaskTree.to_dict()` 直接
  `model_dump()`——pydantic 字段名即序列化键（SCHEMA_VERSION=1）。
  改名须 bump `SCHEMA_VERSION` + 加载时旧键迁移，或显式声明 v0.10
  无跨版本存档兼容（二选一，开工时定）。

**明确排除**：
- 「任务树 = 动态引用视图」（去 `_children_map/_parent_map` 索引、
  按需推导）是结构性重构，归 v0.11 E1；本卡只统一字段。
- `shared_kb.py` 的 `LockInfo.owner_agent_id` 是锁域概念，不涉及。

## 波及面（2026-08-21 实测，改名后数字可能有小幅漂移）
- src 约 32 处：models/task.py、task_tree.py、models/intent.py、
  simulation.py、agent_runtime.py；tests 约 7 处。
- mypy + ruff + 全量 pytest 兜底防漏。

## 难点 / 风险注记
- 无语义风险（值原样迁移）；唯一决策点是持久化旧档兼容处理
  （bump+迁移 vs 声明无兼容，见要求第 4 条）。

## 产出
- 单一引用字段 `derived_from` 的 Task 模型 + 委派路径填充 +
  TaskTree 索引换键 + 持久化 schema 处理 + SPEC §4.2 注记补全
  （"责任字段已落地"旁补引用字段已统一）。

## 完成注记（2026-08-23）
- 持久化决策：**bump SCHEMA_VERSION 1→2 + 显式声明无跨版本存档兼容**。
  理由：既有门禁本就硬拒版本不匹配；若不 bump，v1 存档会被 pydantic
  静默丢弃父链接（extra 字段默认忽略），树结构无痕损坏；项目 0.9.0、
  v0.10 未发布，存储定位为部署内崩溃恢复而非归档。声明写在
  `persistence.py` SCHEMA_VERSION 处。
- 双写收敛：pool 副本（立即/延迟两路）原 parent/derived 同值双写，
  统一后仅留 `derived_from`；effect data 键全路径统一为
  `"derived_from"`（含 None）。
- `_parent_map`/`_children_map` 结构与 cancel 级联/subtree/
  ancestors 行为不变（验收测试全绿佐证）；`Task.child_task_ids`
  字段不在本卡范围，未动。
- 验收 grep 中 `persistence.py` 注释一处提及 `parent_task_id`，
  为 schema 变更记录本身，非任务域代码引用。

## 验收标准
- [x] 任务域不再出现 `parent_task_id`（grep 可验证）
- [x] Task 仅保留单一引用字段 `derived_from`；全部委派路径填充
- [x] 持久化 schema 处理已决策并实现（bump+迁移 或 显式声明）
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过（905 passed）
