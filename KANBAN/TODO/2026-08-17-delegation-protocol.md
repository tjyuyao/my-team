# 委派协议

**Phase:** 2 - E-mail 协作
**Source:** SPEC §7.1, §7.2, §7.3

## 目标

实现 Agent 间通过 E-mail 进行任务委派的完整协议。

## 委派流程

```text
上级 Agent 发送 delegation 邮件
  ↓
子 Agent 收到后发送 acceptance 或 failure 邮件
  ↓
子 Agent 开始执行，可继续向下委派
  ↓
子 Agent 完成后发送 result 邮件
  ↓
上级 Agent 汇总审查
```

## 约束

- 委派只能发送给直接子节点
- 委派任务必须属于委派者当前任务或其子任务
- 不超出委派者权限范围
- 不超过父任务截止时间
- 不违反角色委派上限
- 记录 parent_task_id

## 产出

- 委派邮件生成器
- 接受/拒绝响应处理
- 子任务创建逻辑
- 权限传递校验

## 验收标准

- [ ] Root Agent 能委派给 Research Agent
- [ ] Research Agent 能继续委派给 Web Research Agent
- [ ] Web Research Agent 不能委派给 Planning Agent（非直接子节点）
- [ ] 委派授权满足: 子 Agent 有效权限 ⊆ 委派者有效权限
