---
kind: task
status: completed
phase: 6 - System Integration
source: "review #8; report §7 P1"
priority: high
---

# Typed AgentSnapshot


## 目标

将 `AgentRuntime.observe(snapshot: dict)` 的输入替换为类型化、只读、按 Agent 过滤的 `AgentSnapshot` 数据类。

## 背景

当前 `observe()` 接受普通 `dict`，存在以下风险：

- Agent 可读取未授权数据（其他 Agent 的私人空间）
- 快照 schema 演化不可控
- 类型检查无法防止越权
- 审计和回放困难

## 要求

```python
@dataclass(frozen=True)
class AgentSnapshot:
    tick: int
    agent_id: str
    emails: tuple[EmailView, ...]
    own_tasks: tuple[TaskView, ...]
    private_files: tuple[FileMetadata, ...]
    shared_resources: tuple[SharedResourceView, ...]
    held_locks: tuple[LockView, ...]
```

- `AgentSnapshot` 必须为 frozen dataclass
- 每个 Agent 只能收到过滤后的自身可见数据
- `Simulation._build_snapshot()` 负责构建过滤后的 per-agent 快照
- 更新所有 `AgentRuntime` 子类的 `observe()` 签名

## 产出

- [ ] 新增 `AgentSnapshot` 及相关 view 数据类
- [ ] 修改 `simulation.py` 的快照构建逻辑
- [ ] 更新所有 `observe()` 实现
- [ ] 添加对应测试

## 验收标准

- [ ] Agent 无法通过 `AgentSnapshot` 访问其他 Agent 的 private_files
- [ ] Agent 无法修改 `AgentSnapshot`（frozen）
- [ ] 所有现有测试仍通过
