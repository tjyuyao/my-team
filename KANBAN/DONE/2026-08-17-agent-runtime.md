# AgentRuntime 接口

**Phase:** 6 - 系统集成
**Source:** SPEC §8.2 Phase 3-5, §10
**Priority:** P0
**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

### 产出文件

- `src/my_team/agent_runtime.py` — ToolContext, ToolRegistry, AgentRuntime protocol, BaseAgent/RootAgent/SubAgent/ManagerAgent
- `src/my_team/simulation.py` — Simulation 集成层
- `tests/test_agent_runtime.py` — 30 个测试用例，全部通过

### 实现的功能

1. **ToolContext**: frozen dataclass 绑定 agent_id，防止身份伪造
2. **ToolRegistry**: 工具注册、权限校验、handler 执行
3. **AgentRuntime protocol**: observe/decide/act 三阶段协议
4. **BaseAgent**: 默认实现，可子类化
5. **RootAgent**: 强制限制工具集为 {read, write, ls, delegate}
6. **ManagerAgent**: 含 delegate + send_email
7. **SubAgent**: 基础工具 + 可扩展

### 验收标准

- [x] AgentRuntime 协议定义完成
- [x] ToolContext 绑定 agent_id
- [x] Root Agent 不能调用业务工具
- [x] Agent 不能修改自己的 tools/permissions
