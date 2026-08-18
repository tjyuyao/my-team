---
kind: task
status: completed
phase: v0.10 能力
source: SPEC §14（可靠性）；用户 2026-08-18 需求
priority: medium
---

# v0.10-19: 崩溃防护（连续崩溃检测 + 自动暂停 + 紧急运维回调接口）

**排期：v0.10 次优先级**（与 T18 同域：回滚/可靠性，可同批实现或紧随其后）。

## 背景（需求来源）

用户定稿（2026-08-18）：业务失败不应升格为整回合回滚（归 T18）。
反向的担忧随之而来——**如果系统本身反复触发内核级回滚（连续崩溃），
说明存在系统性缺陷/不变量破坏**。此时继续跑 tick 只会空转烧资源、
反复回滚、污染 Journal。因此需要：

1. **检测**：短时间内连续崩溃事件（整回合回滚/未预期异常）；
2. **动作**：达到阈值自动暂停系统运行（防崩溃循环空转）；
3. **通知**：向 **Provider**（服务提供方）和 **Owner**（系统所有者）发送
   紧急运维请求的**回调接口**（先立接口与空实现 + 测试钩子，真实发送
   后续接邮件/webhook/控制平面）。

## 设计决策（已定，勿在执行时重开）

1. **崩溃定义**：一个 tick 发生整回合回滚（`_last_tick_rolled_back`）或
   `run_tick` 抛出未捕获异常。**局部 FAILED（T18 分级后）不算崩溃**——
   业务失败是常态，不是崩溃。
2. **检测器**：滚动窗口计数（如最近 10 tick 内 ≥ 3 次崩溃），阈值可配
   （SimulationConfig 或 reliability 配置），窗口/阈值进配置而非硬编码。
3. **触发动作**：自动暂停（`SimulationRuntime.pause` 或 HumanControl 暂停
   通道），reason=`crash_guard` + 崩溃详情（窗口内崩溃 tick 列表、末次
   异常）；**暂停后不自动恢复**——需人工显式 resume（避免假恢复循环）。
4. **回调接口**：`CrashGuard` 暴露注册点
   `register_emergency_callback(recipient, handler)`（recipient ∈
   `provider | owner`），触发时逐个调用
   `handler(crash_report)`；默认空实现（log 即可），测试注入探针断言调用。
   CrashReport 至少含：窗口统计、末次异常、epoch、tick。
5. **挂点**：挂在内核回滚/异常路径（`_phase_commit` 回滚分支 + `run_tick`
   外层 try）+ runtime 循环；检测与发送在暂停动作前完成（先通知后暂停）。

## 产出
- `CrashGuard`（或并入 reliability 模块）：滚动窗口检测 + 暂停 + 回调注册。
- Config 扩展：crash 窗口/阈值。
- 测试：连续崩溃触发暂停与回调；单次业务失败（局部 FAILED）不触发；
  resume 后窗口继续滑动。

## 验收标准
- [ ] 连续崩溃（窗口内达阈值）自动暂停，reason=crash_guard，需人工 resume
- [ ] 单次/零星业务失败（局部 FAILED）不触发防护
- [ ] Provider/Owner 回调接口可注册、可注入探针、触发时被调用并收到
      CrashReport
- [ ] 窗口/阈值可配置；实现走配置默认值
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过；kanban_lint 0 violation
## 完成注记（2026-08-18）

实现要点：
- `reliability.py`：`CrashReport`（窗口统计/末次异常/epoch/tick）+ `CrashGuard`
  （滑动窗口计数、`register_emergency_callback(provider|owner)`、`record_crash`
  先通知后暂停、`rearm()` 供 resume 再武装、窗口阈值走构造参数）。
- `SimulationConfig`：`crash_guard_window_ticks`（默认 10）/
  `crash_guard_threshold`（默认 3）。
- 挂点：① `_phase_commit` 内核回滚分支 `record_crash`；② `run_tick` 拆出
  `_run_tick_impl` + 外层 try（未捕获异常计崩溃并 re-raise；已暂停时不计，
  排除 paused 守卫异常）。
- 暂停通道：`Simulation.pause(reason)` 记 `_pause_reason`；`resume()` 清
  reason + guard rearm；tick 中途触发暂停时本轮不再 advance（10 阶段已完成，
  暂停时钟不前进）——顺带修正 pause 与运行中 tick 的边界语义。
- 持久化：`pause_reason` 入 `_collect_state`/load 恢复（crash 窗口本身不
  持久化——重启后重新检测即可）。
- 审计：新增 `SYSTEM_CRASH` / `CRASH_GUARD_TRIGGERED` 两类事件。
- 测试（tests/test_crash_guard.py, 9 个）：连续 3 崩溃触发暂停+双回调+审计；
  单次/零星业务失败（重复 task_id，局部 FAILED）不触发；未捕获异常计崩溃；
  resume 后窗口滑动可再触发；窗口滑动淘汰旧崩溃；配置接线；未知 recipient
  拒绝。
- 文档：SPEC §14 增加崩溃防护不变量。

## 验收核对
- [x] 连续崩溃（窗口内达阈值）自动暂停，reason=crash_guard，需人工 resume
- [x] 单次/零星业务失败（局部 FAILED）不触发防护
- [x] Provider/Owner 回调接口可注册、可注入探针、触发时被调用并收到 CrashReport
- [x] 窗口/阈值可配置；实现走配置默认值
- [x] `uv run pytest -q` 801 passed；`ruff`/`mypy` 通过；kanban_lint 0 violation
