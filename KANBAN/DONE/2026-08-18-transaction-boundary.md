---
kind: task
status: completed
phase: 6 - System Integration
source: "review #12; report §7 P1"
priority: high
---

# Transaction Boundary Clarification


## 目标

明确事务原子性边界，区分 in-memory commit 和 external side effects。

## 背景

当前 `TransactionBuffer` 的 "atomic commit" 仅覆盖内存状态：

```
已覆盖：Task tree states, Email queue, SharedKB resources, Lock states (in-memory)
未覆盖：File system writes, External service calls, Audit log entries, Lock lease timers
```

如果系统在 "commit in-memory" 和 "persist to disk" 之间崩溃，状态丢失。

## 要求

1. 明确文档化事务边界（哪些操作在事务内，哪些是 out-of-band）
2. 定义 external side effects 的提交策略（outbox pattern）
3. Email 不应在事务提交前真正投递
4. 添加事务回滚时的审计事件
5. 添加 `test_transaction_rollback.py` 覆盖回滚路径

## 产出

- [ ] 更新 `transaction.py` 文档注释
- [ ] 实现 outbox 机制（至少邮件）
- [ ] 添加 `test_transaction_rollback.py`

## 验收标准

- [ ] 文档明确列出事务边界内外的操作
- [ ] 事务回滚时邮件不被投递
- [ ] 回滚产生审计事件
