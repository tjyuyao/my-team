# AgentRuntime 接口

**Phase:** 6 - 系统集成
**Source:** SPEC §8.2 Phase 3-5, §10
**Priority:** P0
**Review ref:** 差距 §8.2

## 目标

定义 Agent 运行时协议，使 Agent 能在 Tick 的 Observe/Decide/Act 阶段执行。

## 要求

```python
class AgentObservation(BaseModel):
    agent_id: str
    tick: int
    emails: list[Email]
    task_states: dict[str, Task]
    shared_kb_snapshot: dict[str, Any]
    lock_states: dict[str, Any]

class ActionPlan(BaseModel):
    agent_id: str
    tick: int
    actions: list[AgentAction]

class AgentRuntime(Protocol):
    def observe(self, snapshot: TickSnapshot) -> AgentObservation: ...
    def decide(self, observation: AgentObservation) -> ActionPlan: ...
    def act(self, plan: ActionPlan, context: ActionContext) -> list[ActionResult]: ...
```

- `ToolContext` 绑定 agent_id，工具调用必须携带
- 系统侧从 context 获取身份，忽略请求体中的 agent_id
- Root Agent 的工具集限制为 read/write/ls/delegate

## 产出

- `src/my_team/agent_runtime.py`
- `tests/test_agent_runtime.py`

## 验收标准

- [ ] AgentRuntime 协议定义完成
- [ ] ToolContext 绑定 agent_id
- [ ] Root Agent 不能调用业务工具
- [ ] Agent 不能修改自己的 tools/permissions
