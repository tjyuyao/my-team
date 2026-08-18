---
kind: task
status: completed
phase: 6 - System Integration
source: "review #13; report §3.3"
priority: medium
---

# Conflict Resolution Rules Refinement


## 目标

细化冲突解析规则，区分可合并、可覆盖和必须失败的冲突类型。

## 背景

当前规则：

- 同一 Agent 多个写操作：keep by effect_id order
- 不同 Agent 冲突：alphabetical by agent_id
- Lock holder wins over non-holder

问题：
- "keep by effect_id order" 未说明是全部应用还是只保留一个
- 按 `agent_id` 排序只是确定性，不一定合理
- 非持锁者的 effect 不应被"部分接受"

## 要求

1. 定义冲突类型：可合并、可覆盖、必须失败
2. 非持锁者的写 effect 应直接失败，不参与冲突解析
3. 同一 Agent 的多个写操作按 effect_id 顺序全部应用
4. 不同 Agent 的冲突需要锁或上级决策
5. 更新 `transaction.py` 和测试

## 产出

- [ ] 修改 `transaction.py` 的 `resolve_conflicts`
- [ ] 添加冲突类型定义
- [ ] 更新相关测试

## 验收标准

- [ ] 非持锁者的写 effect 直接失败
- [ ] 同一 Agent 的写操作按顺序应用
- [ ] 不同 Agent 冲突被正确拒绝或合并
