---
kind: task
status: completed
phase: 6 - System Integration
source: "review #9; report §7 P2"
priority: medium
---

# Root Agent Runtime Isolation


## 目标

验证 Root Agent 无法绕过工具授权直接访问系统服务。

## 背景

RootAgent 限制为 `{read, write, ls, delegate}`，但需要确认：

- Root 无法调用 `send_email`
- Root 无法直接写入 SharedKB
- Root 无法直接提交事务 effect
- Root 的 `delegate` 只能发给直接子 Agent

## 要求

1. 编写测试验证 Root 无法调用 `send_email`
2. 编写测试验证 Root 无法直接写入 SharedKB
3. 验证 `delegate` 路径的正确性
4. 添加 `test_root_capability_restrictions.py`

## 产出

- [ ] 添加 `test_root_capability_restrictions.py`
- [ ] 验证 Root 的所有权限边界

## 验收标准

- [ ] Root 调用 `send_email` 被拒绝
- [ ] Root 直接写入 SharedKB 被拒绝
- [ ] Root 只能通过 `delegate` 间接操作
