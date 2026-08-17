# Cross-Process Persistence

**Phase:** Reliability
**Source:** SPEC §5.3; report §7 P3
**Priority:** P3 — Feature

## 目标

实现 simulation 状态的跨进程持久化，支持重启恢复。

## 背景

当前 `PrivateStore` 仅在进程内存中维护状态。系统重启后所有状态丢失。

## 要求

1. 定义持久化格式（JSON 或 SQLite）
2. 实现 `Simulation.save()` 和 `Simulation.load()`
3. 持久化内容：agent state, task tree, email queue, shared KB, lock states
4. 添加 `test_persistence_restart.py` 覆盖重启恢复场景
5. 添加 `test_full_state_replay.py` 覆盖状态回放

## 产出

- [ ] 持久化模块（`persistence.py` 或 `storage.py`）
- [ ] `Simulation.save()` / `Simulation.load()`
- [ ] `test_persistence_restart.py`
- [ ] `test_full_state_replay.py`

## 验收标准

- [ ] save/load 后状态一致
- [ ] 重启后 simulation 从中断点继续
- [ ] 回放产生相同结果
