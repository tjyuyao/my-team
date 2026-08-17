# 身份强制与权限执行

**Phase:** 6 - 系统集成
**Source:** SPEC §15.1, §15.2
**Priority:** P1
**Review ref:** 差距 §8.3, §8.13

## 目标

确保 Agent 不能伪造身份、不能绕过权限。

## 要求

### ToolContext

```python
class ToolContext:
    agent_id: str          # 由系统绑定，不可由 Agent 设置
    simulation_id: str
    tick: int
    allowed_tools: frozenset[str]
```

### 身份绑定

- 邮件发送: `MailSystem.send(sender_context, ...)` — 系统设置 `from_agent`
- 文件操作: `FileOps.read(context, path)` — 系统验证 context.agent_id
- 共享 KB: `SharedKB.write(context, ...)` — 系统验证权限

### 禁止行为

- Agent 不能修改自己的 `tools` 字段
- Agent 不能修改自己的 `shared_kb_permissions`
- Agent 不能设置 `from_agent` 为其他 Agent
- 委派时权限可缩减不可扩大

## 产出

- `src/my_team/agent_runtime.py` (ToolContext)
- `tests/test_identity_security.py`

## 验收标准

- [ ] 伪造 from_agent 被系统拒绝
- [ ] 越权文件访问被拒绝
- [ ] Root Agent 不能调用业务工具
- [ ] Agent 不能修改自身权限
