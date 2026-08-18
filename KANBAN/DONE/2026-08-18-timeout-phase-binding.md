---
kind: task
status: completed
phase: 5 - Reliability
source: SPEC §8.2; report §7 P2
priority: medium
---

# Timeout Checker Phase Binding


## 目标

将超时检查绑定到确定性的 tick 阶段。

## 背景

当前超时检查的执行时机不明确。SPEC §8.2 要求确定性时序。

## 要求

1. 超时检查绑定到 Commit 阶段之后（Phase 6 → Phase 7 之间）
2. `TimeoutChecker.check()` 接收当前 tick 号
3. 过期锁在超时检查时自动释放
4. 过期任务在超时检查时标记 `TIMED_OUT`
5. 更新 `reliability.py` 和 `tick_engine.py`

## 产出

- [ ] 修改 `tick_engine.py` 在 Audit 阶段前调用 `TimeoutChecker`
- [ ] 修改 `reliability.py` 的超时检查逻辑
- [ ] 添加 `test_timeout_boundaries.py`

## 验收标准

- [ ] 超时检查在 Commit 之后、Audit 之前执行
- [ ] 过期锁被自动释放
- [ ] 过期任务被标记 `TIMED_OUT`
