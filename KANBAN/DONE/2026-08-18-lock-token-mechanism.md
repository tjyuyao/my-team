---
kind: task
status: completed
phase: 6 - System Integration
source: "SPEC §6.3; review #21; report §7 P1"
priority: high
---

# Lock Token Mechanism


## 目标

为 `LockManager` 添加 UUID-based lock token，防止 stale-holder 攻击。

## 背景

当前释放锁仅依赖 `release(resource, agent_id)`。攻击场景：

1. Agent A 在 tick 5 获取锁
2. Agent A 的 lease 在 tick 9 过期
3. Agent B 在 tick 10 获取同一锁
4. Agent A 的延迟 `release()` 到达 — 检查 `owner_agent_id == "agent.a"` 但锁已属于 Agent B
5. **结果：Agent B 的锁被错误释放**

## 要求

- `LockInfo` 新增 `lock_token: str` 字段（UUID4）
- `acquire()` 返回 `LockInfo`（含 token）
- `release()` 要求同时匹配 `agent_id` 和 `lock_token`
- `renew()` 要求 `lock_token` 验证
- 已过期锁的 release 调用应被拒绝
- 添加对应单元测试覆盖 stale-holder 场景

## 产出

- [ ] 修改 `shared_kb.py` 中的 `LockInfo`、`LockManager`
- [ ] 添加 `test_lock_token.py` 测试文件
- [ ] 验证 stale-holder 攻击场景被阻断

## 验收标准

- [ ] Agent A 释放过期锁时抛出 `LockError`
- [ ] Agent B 持有锁时，Agent A 的旧 release 不影响 Agent B
- [ ] renew 需要 token 验证
- [ ] 所有现有锁测试仍通过
