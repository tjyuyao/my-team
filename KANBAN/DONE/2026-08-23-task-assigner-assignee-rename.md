---
kind: task
status: completed
phase: v0.10 任务模型
source: SPEC §4.2；KANBAN/DONE/scheduler-calendar-pool 实现注记
priority: medium
---

# Task 责任字段改名：creator/owner → assigner/assignee

**Status:** DONE
**Completed:** 2026-08-23

## 目标
按任务模型定稿（SPEC §4.2，2026-08-19 用户定稿）对齐责任术语：
`assigner`（委派方）/ `assignee`（责任方）。纯机械改名，无行为变更。

## 要求 / 规则
- 字段名带 `_agent_id` 后缀以符合既有命名约定：
  `Task.creator_agent_id` → `Task.assigner_agent_id`；
  `Task.owner_agent_id` → `Task.assignee_agent_id`
  （概念术语为 assigner/assignee；若实现时倾向裸名需同步修订
  SPEC §4.2 措辞——默认带后缀）。
- `TaskTree` API 同步：`create()` 参数、`get_owner_tasks()` →
  `get_assignee_tasks()`、内部 `_owner_map` → `_assignee_map`。
- simulation 暂存 effect 的 data 键（`"creator_agent_id"` /
  `"owner_agent_id"`）与 snapshot 的 `"owner"` 键同步改名。
- **明确排除**：`shared_kb.py` 的 `LockInfo.owner_agent_id` 是**锁域**
  概念（锁持有者），不属任务责任链，不改名。
- 完成时更新 SPEC §4.2 末尾的「术语对齐（实现迁移）」注记为已落地。

## 波及面（2026-08-21 实测）
- src 6 文件约 40 处：models/task.py、task_tree.py、simulation.py、
  reliability.py、human_control.py、control_plane.py。
- tests 约 76 处、18+ 文件。
- mypy + ruff + 全量 pytest 兜底防漏。

## 难点 / 风险注记
- 无语义风险；唯一风险是漏改或误改锁域 owner——用排除清单 +
  全量测试兜底。

## 产出
- 改名后的模型/API/调用点 + 测试迁移 + SPEC 注记更新。

## 完成注记（2026-08-23）
- 落地面较立卡实测多 2 处：context_compiler.py 消费 snapshot 任务键
  `owner`（TaskScope OWNED/SUBTREE 过滤 + pending_decisions 输出），
  随 snapshot 键改名同步；reliability.py 任务超时 audit details 键
  `owner` → `assignee`。
- 快照持久化键 `"owner_map"` → `"assignee_map"`，存取两侧同改，
  test_persistence / test_tick_journal 全绿。
- 验收 grep：任务域 0 处旧名；锁域保留 4 处
  （shared_kb.py + simulation/reliability 的 `lock.owner_agent_id`）。

## 验收标准
- [x] 任务域不再出现 `creator_agent_id` / `owner_agent_id`
      （shared_kb 锁域除外，grep 可验证）
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
      （902 passed；3 个 run_tests 子进程失败为沙箱环境预存问题，
      干净 HEAD 上同样失败）
- [x] SPEC §4.2 术语注记更新为已落地
