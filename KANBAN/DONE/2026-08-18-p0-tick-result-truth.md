# P0-3: run_tick 返回真实内核结果，TickEngine 降为纯时钟

**Phase:** v0.9 P0
**Source:** SPEC §3.1、§14；OI-003 P0-3/P1-4
**Priority:** high
**Completed:** 2026-08-18
**Tests:** 768 passed（+7），ruff clean，mypy clean

## 目标
`Simulation.run_tick()` 的返回值必须反映真实 10 阶段内核：
`phases_completed`、`committed`、`errors` 与 Journal 一致；
TickEngine 不再执行任何 7 阶段 stub 循环。

## 要求 / 规则
- `run_tick` 直接构造 `TickResult`，以 `_last_tick_phases` 与
  `_last_tick_rolled_back` 为准。
- 回滚时 `committed=False`，`errors` 包含回滚原因。
- 删除或退役 TickEngine 的 phase handlers 与 `_execute_tick`；
  保留 `current_tick`、`state`、`pause/resume/advance` 作为纯时钟。
- `last_tick_phases` 必须包含真实阶段（统一为 10 阶段名，把
  Deliver 明确并入 Ingest 或作为独立阶段，二选一并全仓库一致）。

## 产出
- 真实的 TickResult。
- 单一时钟/阶段模型；SPEC §3.1 与代码一致。

## 验收标准
- [x] 回滚 tick 返回 `committed=False` 且 `errors` 非空
- [x] `phases_completed == sim.last_tick_phases`
- [x] `TickEngine` 不再包含 7 阶段循环调用
- [x] 新增 `test_tick_result_truth` 覆盖成功/回滚两种情况（+7 tests）
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
