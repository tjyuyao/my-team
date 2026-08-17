# 超时与重试

**Phase:** 5 - 可靠性
**Source:** SPEC §14.1, §14.2, §14.4

## 目标

实现 Agent 执行失败、子任务超时和邮件投递失败的处理。

## Agent 执行失败

- 回滚未提交动作
- 保留已提交的 E-mail
- 自动重试（指数退避）
- 标记任务为 blocked
- 通知父 Agent
- 请求人类介入

## 子 Agent 超时

1. 任务标记为 expired
2. 向任务所有者发送 system_notice
3. 任务所有者重试、重新委派或降级
4. 父任务不自动标记完成

## 邮件投递失败

```text
queued → delivery_failed → retrying → delivered
                          └── permanently_failed
```

策略: 指数退避、最大重试次数、超限通知发件人

## 产出

- 重试管理器
- 超时检测器
- 失败通知逻辑
- 指数退避实现

## 验收标准

- [ ] Agent 失败后自动重试
- [ ] 子任务超时正确标记并通知
- [ ] 邮件投递失败按策略重试
