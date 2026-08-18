---
kind: task
status: completed
phase: v0.9 基础
source: SPEC §2、§13；OI-004 §1.3
priority: high
---

# v0.9-5: SimulationRuntime、Control Plane 与 LLM dispatcher

**Completed:** 2026-08-18
**Tests:** 751 passed（+57），ruff clean，mypy clean（42 source files）

## 目标
系统从"同步紧循环"变为可实时运行、可调速、可外部控制的服务；
LLM pending op 有真实 dispatcher 执行，不再依赖测试 harness 手动
完成。

## 要求 / 规则
- `SimulationRuntime`：wall-clock 循环，每 tick 之间按
  `tick_duration` 睡眠；每个 tick 边界应用 pending duration changes。
- 提供 start/pause/resume/step/set_tick_duration 控制接口。
- Control Plane 最小 HTTP API（FastAPI 或标准库 http.server）：
  status、start/pause/resume/step、发送人类消息、待审批列表。
- LLM dispatcher：worker 线程/进程轮询 SUBMITTED LLM op，调用
  LLMGateway，结果写回 op（COMPLETED）供下一 tick Ingest。
- 暂停语义：暂停中不 run_tick；外部结果与入站事件进入隔离区，
  resume 后下一 tick 处理。
- FakeProvider 保留用于确定性测试；真实 dispatcher 用 fake gateway
  做集成测试。

## 产出
- Runtime 与最小控制 API。
- LLM dispatcher（可与 Runtime 同进程线程）。

## 验收标准
- [ ] `runtime.start()` 后 tick 按 wall-clock 推进（可测短 duration）
- [ ] `set_tick_duration` 在下一 tick 生效
- [ ] 暂停中 run_tick 不可调用；恢复后继续
- [ ] LLM op 由 dispatcher 自动完成，无需测试手工 complete
- [ ] HTTP API 可查看状态与发送人类消息
- [ ] 新增集成测试覆盖以上；`uv run pytest -q` 全绿
