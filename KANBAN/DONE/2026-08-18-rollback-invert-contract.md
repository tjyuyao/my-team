---
kind: task
status: completed
phase: v0.10 能力
source: SPEC §3.3（事务与回滚，已同步）、§8.2（补偿）
priority: medium
---

# v0.10-18: 回滚逆操作契约（explicit invert contract）

**排期：v0.10 次优先级**（不阻塞 T8a/T10；可在工具执行环境对齐卡之前或之后）。

## 背景（为什么需要这张卡）

回滚现状是**隐式分散**的：文件用 `file_previous`（提交前记旧内容/None）、
KB 用 `kb_state_before`（记旧 resource+version）、邮件用 outbox 丢弃（阻止
未派发副作用）、pending op 用反注册——全部散落在 `_phase_commit` 的回滚
循环里，没有"每个 EffectType 声明自己的逆操作"这一统一契约。

原则（用户定稿 + SPEC §3.3）：**回滚不靠状态快照，靠每个 effect 的逆操作
（撤回语义）**。对本地资源，逆操作 = 前值恢复（记录旧值，回滚写回）；
对外部资源，让外部系统提供状态引用/checkpoint 几乎不可能，正确形态是
每个 effect/请求**声明逆操作（补偿）**——系统的 `reversible` manifest 字段
与 SPEC §8.2 补偿工具已经为此铺路，但内核回滚路径尚未把它们统一成契约。

## 目标
- 每个 EffectType 声明 invert 契约：**记录什么前值 + 如何恢复**；
  EXTERNAL 类 effect 的 invert = 补偿工具引用或"不可逆"标记。
- 回滚路径收敛为**单一入口**：逐 committed effect 执行其逆操作。
- **失败分级显式化**（用户 2026-08-18 定稿）：只有系统级不变量破坏
  （apply 抛未预期异常）才触发整回合回滚；可判定业务失败（权限/锁/版本/
  patch 冲突/重复 task_id 等）一律**局部 FAILED**，其余 effect 照常提交，
  不回滚世界。现状的隐式分级（靠"抛不抛异常"）改为显式声明：
  每个 effect 的失败点声明"可判定失败 → FAILED"或"内核失败 → 回滚"。
  **既定范围（用户已确认）**：`task_tree.create` 对重复 task_id 的 raise
  降级为可判定失败（局部 FAILED + group 原子性），不再拉全回合回滚；
  同步更新相关回滚用例断言（test_file_write_rollback 等）。
- 行为语义不变（文件/KB/邮件/pending op 的**内核级**回滚结果与现状
  一致），只把机制从隐式散落整理为显式契约 + 失败分级。

## 实施步骤
1. `transaction.py`：`EffectType` 侧新增 invert 定义表
   `{effect_type: (记录体字段, 恢复动作描述)}`；`StagedEffect` 增加
   `invert_data: dict`（提交时写入前值：文件旧内容、KB 旧 state、锁旧
   owner、task 旧状态等），替代散落的 file_previous / kb_state_before。
2. `simulation.py _phase_commit`：收集前值改写入 invert_data；回滚分支改为
   统一循环 `for effect in committed: invert(effect)`，删除
   file_previous/kb_state_before 独立字典。
3. 外部 effect（现 EMAIL_SEND 属 outbox 丢弃型；将来 EXTERNAL_* 类）：
   invert 定义为"未派发即丢弃"或"补偿工具引用/不可逆标记"；不可逆的
   回滚时如实标 FAILED 并记审计（不静默吞掉）。
4. 测试：现有回滚用例（文件恢复/删除新文件/KB 恢复/锁恢复/op 反注册/
   邮件丢弃）全数保留并断言走 invert 路径；新增契约一致性测试
   （每个已实现的 effect_type 都有 invert 定义）。
5. by-product：确认`_phase_rollback` 与 `_phase_commit` 单入口；文档同步
   SPEC §3.3（若需要）。

## 验收标准
- [ ] 回滚为单一入口，逐 effect 执行逆操作；无 file_previous/kb_state_before
      独立字典
- [ ] 每个已实现 EffectType 有 invert 定义（契约一致性测试）
- [ ] 失败分级显式：可判定失败局部 FAILED（其余照常提交）；仅内核异常
      触发整回合回滚（测试覆盖两类路径）
- [ ] 行为不变：文件恢复、新文件删除、KB 恢复、锁/op/邮件回滚语义与
      现状一致（现有回滚测试全绿，或已按分级决策显式更新）
- [ ] 外部 effect 的 invert 语义明确（丢弃/补偿/不可逆标记）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过；kanban_lint 0 violation
## 完成注记（2026-08-18，与 T20 同批）

实现要点：
- `transaction.py`：`InvertKind`（RESTORE_PREVIOUS / REMOVE_CREATED /
  UNREGISTER / IRREVERSIBLE）+ `InvertSpec` + `INVERT_CONTRACT`（覆盖全部
  14 个 EffectType）+ `StagedEffect.invert_data`。
- `_phase_commit`：apply 循环逐 effect 写 invert_data（文件前值 = 旧内容或
  None、KB 前值 = resource+version 深拷贝、outbox entry_id、task 前值），
  删除 `file_previous` / `kb_state_before` / `created_task_ids` 独立字典；
  `_rollback` 收敛为单一入口：释放本 tick 锁 → `reversed(applied)` 逐 effect
  `_invert_one` → pending op 反注册 → continuation 恢复。TASK_UPDATE 回滚
  补齐（此前未实现，现记录 task_state_before 深拷贝）。
- 失败分级显式化：TASK_CREATE 重复/父缺失、TASK_UPDATE 无效状态/不可达
  转移改为 apply 预检 + `_fail_locally`（FAILED 局部，不回滚）；group 原子性
  在 apply 期局部失败同样成立（成员整体 FAILED，已应用成员逐个逆操作撤销）；
  FILE_PATCH stale / 路径拒绝也走 `_fail_locally`。仅 apply 抛未预期异常走
  整回合回滚，`failing_effect` 精确标记失败 effect（替代 committed[-1]）。
- 测试：回滚触发方式由"重复 task_id raise"改"目标路径为目录的 FILE_WRITE
  （IsADirectoryError）= 真内核失败"；新增契约一致性 + group 局部失败 + 争锁/
  重试/回滚释放锁用例（tests/test_invert_contract.py, 5 个）；全量 792 passed；
  mypy clean；ruff 通过；kanban_lint 0 violation。
- 文档：SPEC §3.3 写入 invert 契约 + 失败分级；SIMULATION_MAP 上帝对象注记
  更新（T18 已完成）。

## 验收核对
- [x] 回滚为单一入口，逐 effect 执行逆操作；无 file_previous/kb_state_before 独立字典
- [x] 每个已实现 EffectType 有 invert 定义（契约一致性测试 test_every_effect_type_has_invert_definition）
- [x] 失败分级显式：可判定失败局部 FAILED（其余照常提交）；仅内核异常触发整回合回滚（两类路径均有测试）
- [x] 行为不变：文件恢复、新文件删除、KB 恢复、任务删除、op 反注册、邮件丢弃语义一致（回滚用例全绿）
- [x] 外部 effect 的 invert 语义明确（EMAIL 未派发丢弃；IRREVERSIBLE 标记 + 审计）
- [x] `uv run pytest -q` 792 passed；`ruff`/`mypy` 通过；kanban_lint 0 violation
