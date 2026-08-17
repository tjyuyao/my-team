# 人类 E-mail

**Phase:** 4 - 人类控制
**Source:** SPEC §12.4

## 目标

实现人类用户向 Agent 发送 E-mail 的功能。

## 邮件格式

```json
{
  "email_type": "human_message",
  "from": "human.user_001",
  "to": ["agent.root"],
  "subject": "补充要求",
  "body": "请优先考虑成本约束。",
  "deliver_at_tick": 18
}
```

## 规则

- 进入 Agent 正常邮箱
- 不能绕过权限直接修改 Agent 状态
- 可以要求 Agent 执行任务
- 是否具有更高优先级由策略决定
- 所有内容写入审计日志

## 产出

- 人类邮件 API
- 邮件注入逻辑
- 优先级策略配置

## 验收标准

- [ ] 人类能向 Root Agent 发送邮件
- [ ] 邮件在指定 tick 投递
- [ ] 邮件内容写入审计日志
