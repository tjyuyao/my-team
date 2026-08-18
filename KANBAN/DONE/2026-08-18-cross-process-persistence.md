---
kind: task
status: completed
phase: Reliability
source: SPEC §5.3; report §7 P3
priority: low
---

# Cross-Process Persistence

**Status:** ✅ 核心已交付（v0.6.0）；剩余并入 v0.8.0 计划（2026-08-17 关闭）

## 目标

实现 simulation 状态的跨进程持久化，支持重启恢复。

## 交付情况（v0.6.0）

- [x] 持久化模块：`persistence.py` → `SimulationStore`（SQLite）
- [x] `Simulation.save()` / `Simulation.load()`
- [x] 状态一致性与崩溃恢复：`test_persistence.py`
  （TestSimulationStore / TestSaveLoadRoundtrip / TestCrashRecovery /
  TestLoadErrors）

## 剩余（并入 v0.8.0，见 KANBAN/TODO/2026-08-17-v080-implementation-plan.md）

- [ ] 跨重启继续执行语义（SUBMITTED/PENDING ops 重启后可被外部
      执行器完成、结果正常 ingest）→ **v0.8.0 P1-1**
- [ ] 回放等价性 / 跨进程恢复测试（worker 崩溃、多次重启收敛）→
      **v0.8.0 P2-8/P2-9**

> 原计划中的 `test_persistence_restart.py` / `test_full_state_replay.py`
> 文件名未按原样交付；重启/回放场景由 test_persistence.py 的
> TestCrashRecovery 覆盖，剩余语义在 v0.8.0 P1-1/P2-9 中明确。
