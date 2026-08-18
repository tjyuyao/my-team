---
kind: issue
status: open
---

# OI-002: 持久化不保存 provider 脚本位置（外部 harness 状态）

**Opened:** 2026-08-17 (P3-11 实现)
**Status:** OPEN — 设计使然，文档化

## 问题

`FakeLLMProvider` 的脚本游标（`_call_counters`）不属于模拟状态，
`save_to()`/`load_from()` 不保存它。恢复后 provider 从脚本第 0 条
重新开始，而 continuation 可能已处于第 N 次调用的等待中。

## 影响

测试 harness 必须在 load 后重新对齐脚本（读取原 provider 的
`_call_counters` 续接）；真实 LLM provider 无此问题（无脚本概念）。
行为对齐由 `test_persistence.py::test_deterministic_lockstep` 验证。

## 为什么不现在解决

provider 属于测试/外部 harness，不属于 Simulation 的内核状态。
把 harness 状态塞进内核 DB 会破坏关注点分离。

## 触发条件

- 若未来需要"跨重启的脚本化 E2E 完全无缝"，可在 harness 层
  （非内核）持久化 provider 计数器
