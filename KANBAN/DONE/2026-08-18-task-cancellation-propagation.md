---
kind: task
status: completed
phase: 2 - Email Collaboration
source: "review #20; report §7 P2"
priority: medium
---

# Task Cancellation Propagation


## 目标

实现父任务取消时的子任务级联取消逻辑。

## 背景

当前父任务取消时，子任务不受影响。需要确定取消策略：

### 推荐方案：级联取消

```
父任务 cancelled
→ 所有未完成子任务 cancelled
→ 生成审计事件
```

## 要求

1. `TaskTree.cancel_task()` 实现级联逻辑
2. 递归取消所有未完成（非 `COMPLETED`/`CANCELLED`）的子任务
3. 每次取消生成审计事件
4. 已完成的子任务不受影响
5. 添加 `test_task_cancellation_cascade.py`

## 产出

- [ ] 修改 `task_tree.py` 的 `cancel_task` 方法
- [ ] 添加级联取消测试
- [ ] 验证审计事件完整

## 验收标准

- [ ] 父任务取消时，所有未完成子任务状态变为 `CANCELLED`
- [ ] 已完成子任务不受影响
- [ ] 每次取消产生审计事件
