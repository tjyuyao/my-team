---
kind: task
status: completed
phase: Core Runtime
source: SPEC §8.4; tick semantics discussion; report §7 P3
priority: high
---

# LLM Agent Integration


## 目标

实现基于 LLM 的 AgentRuntime 实现，使 Agent 能够进行推理。

## 背景

当前 Agent 是规则型/callback 驱动的，没有真正的推理能力。`decide()` 只返回预定义的 `ActionPlan`。

讨论结论：采用**自研 Simulation Kernel + LiteLLM 作为 LLM Gateway** 的架构。

## 架构

```text
Simulation Kernel (自研)
  → Agent Scheduler (自研)
    → AgentRuntime (自研)
      → LLM Gateway (LiteLLM)
        → Provider (OpenAI / Anthropic / Gemini / 本地模型)
```

### 职责划分

| 组件 | 职责 | 不负责 |
|------|------|--------|
| Simulation Kernel | tick 调度、事件、事务、审计 | LLM 调用细节 |
| Agent Runtime | observe/decide/act 协议 | 全局 tick 管理 |
| LLM Gateway | 多供应商适配、retry/fallback、cost tracking | Agent 组织树、邮件、任务 |
| LangGraph（可选） | 单 Agent 内部复杂工作流 | 全局 Simulation |

## 要求

### LLM Gateway

1. 使用 LiteLLM 作为 LLM Provider Gateway
2. 统一 OpenAI / Anthropic / Gemini / 本地模型接口
3. 支持 retry / fallback / cost tracking / token usage
4. 支持 streaming 和 structured output

### LLM Agent 实现

5. 实现 `LLMAgent` 类，实现 `AgentRuntime` 协议
6. `observe()` 将 `AgentSnapshot` 转换为 LLM prompt
7. `decide()` 调用 LLM API 获取决策
8. `act()` 执行 LLM 推荐的动作（通过显式 Action）

### 工具调用

9. LLM 工具调用必须经过 ToolRegistry 授权
10. LLM 不能直接写数据库或文件
11. LLM 输出只能产生 ActionPlan，不能直接修改系统状态
12. 工具调用作为显式 Action 提交，不是同步调用

### Prompt 工程

13. 定义 prompt 模板（system prompt + observation → LLM input）
14. 支持 tool use 格式（function calling）
15. 支持 structured output（JSON schema）

### 配置

16. 支持配置不同 LLM 后端
17. 支持配置 model、temperature、max_tokens 等参数
18. 支持配置 API key 和 endpoint

### LLM Invocation 追踪

19. **LLMInvocation** 数据模型：
    - invocation_id, activation_id, agent_id
    - model, input_hash, created_at_tick
    - status: pending / completed / failed / timeout
    - completed_at_tick, token_usage

20. 每次 LLM 调用记录完整输入和输出
21. 记录 model、参数、token usage

## 产出

- [ ] `llm_gateway.py` — LiteLLM Gateway 封装
- [ ] `llm_agent.py` — LLM-backed AgentRuntime 实现
- [ ] `models/llm.py` — LLMInvocation + TokenUsage 数据模型
- [ ] `prompts/` — prompt 模板目录
- [ ] `configs/llm_config.json` — LLM 配置 schema
- [ ] `test_llm_gateway.py` — Gateway 单元测试（mock provider）
- [ ] `test_llm_agent.py` — LLM Agent 测试
- [ ] 集成测试

## 依赖

- `litellm` 作为 LLM Gateway
- 需要先完成 Agent Activation/Scheduling（TODO #17）
- 可选：LangGraph 用于复杂 Agent 内部工作流（第二阶段）

## 验收标准

- [ ] LLM Agent 能观察环境并做出决策
- [ ] 工具调用正确执行且经过授权
- [ ] LLMInvocation 正确记录每次调用
- [ ] 与现有系统集成正常
- [ ] 支持多种 LLM 后端切换
- [ ] cost tracking 正确
