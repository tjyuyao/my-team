# LLM Agent Integration

**Phase:** Core Runtime
**Source:** report §7 P3
**Priority:** P3 — Feature

## 目标

实现基于 LLM 的 AgentRuntime 实现，使 Agent 能够进行推理。

## 背景

当前 Agent 是规则型/callback 驱动的，没有真正的推理能力。`decide()` 只返回预定义的 `ActionPlan`。

## 要求

1. 实现 `LLMAgent` 类，实现 `AgentRuntime` 协议
2. `observe()` 将 `AgentSnapshot` 转换为 LLM prompt
3. `decide()` 调用 LLM API 获取决策
4. `act()` 执行 LLM 推荐的动作
5. 支持工具调用（tool use）格式
6. 添加配置支持不同 LLM 后端

## 产出

- [ ] `llm_agent.py` 模块
- [ ] LLM prompt 模板
- [ ] 工具调用解析
- [ ] 配置 schema
- [ ] 集成测试

## 验收标准

- [ ] LLM Agent 能观察环境并做出决策
- [ ] 工具调用正确执行
- [ ] 与现有系统集成正常
