---
kind: task
status: completed
phase: 5 - 可靠性
source: SPEC §14.3
---

# 锁租约

**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

已在 Phase 3 (shared_kb.py) 中实现。

### 实现的功能

- LockManager: acquire/release/renew/check_expired
- 默认租约 4 ticks
- 超时自动释放 + 审计日志
- TimeoutChecker 集成

### 验收标准

- [x] 租约到期自动释放锁
- [x] 超时产生审计事件
- [x] 通知锁持有者和等待者
- [x] Agent 可重新获取锁
