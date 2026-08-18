---
kind: task
status: completed
phase: 6 - System Integration
source: "review #14"
priority: medium
---

# SharedKB Double-Commit Path Fix


## 目标

消除 Agent 绕过 TransactionBuffer 直接写入 SharedKB 的路径。

## 背景

如果 Agent 可以同时调用 `shared_kb.write(...)` 和生成 `WriteEffect(...)`，则存在两条写入路径，可能绕过事务缓冲。

## 要求

1. 定义唯一修改入口：`SharedKB.commit_mutation(transaction, effect)`
2. 普通 Agent 不持有原始 `SharedKB` 对象
3. Agent 通过 `IdentityEnforcer.wrap_shared_kb_write` 间接操作
4. 添加验证测试确认 Agent 无法直接写入

## 产出

- [ ] 修改 SharedKB 接口，添加事务感知的写入方法
- [ ] 验证 Agent 无法绕过 TransactionBuffer
- [ ] 添加对应测试

## 验收标准

- [ ] Agent 只能通过事务路径写入 SharedKB
- [ ] 直接调用 `shared_kb.write()` 被拒绝或记录审计事件
