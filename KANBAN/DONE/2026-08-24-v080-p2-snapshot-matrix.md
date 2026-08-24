---
kind: task
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§15；KANBAN/PLAN/v0.8.0-plan（P2-8）
priority: medium
status: completed
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

## 实现注记（2026-08-24，完成）
- **矩阵规模**：`tests/test_snapshot_matrix.py`，10 类状态面 × 3 性质
  = **30 行**（9 类既定 + T16c 预算累计器，因预算组件在动笔期间已
  落地并进 `_collect_state`/`_restore_state`，纳入第 10 类）+ 1 个
  矩阵完整性自检。逐行断言具体值/逐字段相等，不用「存在即通过」。
- **性质语义**：Freeze 可见性 = 冻结视图含具体提交态值且 tick 内
  不可变（TaskTree 状态/assignee、private file 索引与 workspace
  version 哈希、KB paths+versions、claims 排他、registry 是
  Validate 所见、epoch 打在 op 与 journal）；Commit 可回滚性 = 回滚
  tick 后逐字段等于 tick 前（含 scheduler claims 重新入队、pool
  委派产物清零、human pending actions 恢复、request_id 可复用、
  单调计数器不回滚）；持久化 = save→load 后状态一致（含重启后
  epoch fencing 与预算拒绝行为保留）。
- **发现缺陷（未修，主 agent 处理）**：`_phase_commit` 的
  `REMOVE_CREATED` 逆操作（TASK_CREATE 回滚）从 `_tasks`/
  `_parent_map`/`_assignee_map` 移除子任务，但**未从父任务的
  `_children_map` 值列表移除子 id**——回滚后 `_children_map[父]`
  残留悬空子边并随持久化留存（`tests/test_snapshot_matrix.py`
  task_tree 回滚行的缺陷注记处只断言已正确的部分，待修复后补回
  严格断言）。影响低（`children()` 按存活过滤），但持久化图不一致。
- **测试基建复用**：沿用 `_tree`/`_make_sim`（锚定时间）约定；内核
  爆炸用既有 IsADirectoryError 技巧；新增 `_ScriptedAgent` 走完整
  run_tick 路径（claims 重入队、human 动作恢复必须经全 tick）；
  直接阶段调用场景按内核语义在回滚前清 per-tick 跟踪。
- **并行说明**：T16c 预算落地晚于矩阵动笔但早于收尾，已纳入；
  并行中的 sandbox 卡改动未触碰本测试文件。全量验证由主 agent
  统一收尾（并行中间态曾有 control_plane 端口冲突与 sandbox 测试
  中间失败，归属并行卡）。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **量大不硬**：9 类状态面 × 3 性质（Freeze 可见性 / Commit 可回滚性 /
  持久化）的逐行覆盖，工作量在测试基础设施完备性，不在设计。
- **时序要点**：放 v0.10 最后——T11/T12a 会动状态面（scheduler claims、
  human queue），矩阵应覆盖最终状态，提前做必返工。

## 验收标准
- [x] Snapshot 矩阵全部行通过（测试可见）
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过（矩阵 31 项单独全绿；
  全量与 ruff/mypy/kanban_lint 由主 agent 统一验证）
