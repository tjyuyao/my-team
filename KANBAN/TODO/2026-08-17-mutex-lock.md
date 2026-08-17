# 排他锁

**Phase:** 3 - 共享知识库
**Source:** SPEC §6.3

## 目标

实现共享知识库写操作的互斥锁机制。

## 锁属性

```json
{
  "lock_id": "lock.001",
  "resource": "project/research/market-report.md",
  "owner_agent_id": "agent.research",
  "mode": "exclusive",
  "acquired_at_tick": 12,
  "lease_until_tick": 16,
  "status": "active"
}
```

## 锁规则

1. 同一资源最多一个排他写锁
2. 必须先获取锁才能写
3. 锁有租约期限
4. 可在到期前续租
5. 完成后主动释放
6. Agent 失败则租约到期自动释放
7. 未持锁写请求必须失败
8. 锁冲突通过系统 E-mail 通知

## 产出

- 锁管理器
- 获取/释放/续租 API
- 租约超时处理
- 锁冲突通知

## 验收标准

- [ ] 同一资源不能同时被两个 Agent 持有排他锁
- [ ] 租约到期自动释放
- [ ] 锁冲突产生系统通知邮件
