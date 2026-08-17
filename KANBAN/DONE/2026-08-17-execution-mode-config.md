# Execution Mode Configuration

**Phase:** Core Runtime
**Source:** SPEC §8.5; tick semantics discussion
**Priority:** P2 — Feature

## 目标

实现可配置的执行模式，支持离散异步模式（默认）和有界微循环模式。

## 背景

SPEC §8.5 定义了两种执行模式：
- 模式 A（离散异步）：每次 LLM/工具动作跨 tick 执行
- 模式 B（有界微循环）：一个 activation 内允许有限次 LLM → Tool 循环

当前实现是同步执行所有 action，没有模式区分。

## 要求

### 配置模型

1. **ExecutionConfig** 数据模型：
   ```python
   execution_mode: str = "discrete_async"  # or "bounded_micro_loop"
   max_llm_calls_per_activation: int = 1
   max_tool_calls_per_activation: int = 8
   max_action_budget: int = 32
   max_rounds: int = 3  # only for bounded_micro_loop
   ```

### 离散异步模式（默认）

2. 每次 activation 只执行一次 LLM 调用
3. 工具调用作为显式 Action 提交
4. 工具结果在后续 tick 通过 WakeupEvent 返回
5. Agent 再次被唤醒时读取工具结果

### 有界微循环模式

6. 一个 activation 内允许有限次 LLM → Tool 循环
7. 循环次数受 `max_rounds` 限制
8. 每轮循环都记录审计事件
9. 超出预算时强制停止并等待下一 tick

### ToolInvocation 追踪

10. **ToolInvocation** 数据模型：
    - call_id, activation_id, agent_id
    - tool_name, arguments_hash
    - requested_at_tick, completed_at_tick
    - status: pending / completed / failed / timeout

11. 工具调用必须经过系统授权
12. LLM 不能直接写数据库或文件
13. LLM 输出只能产生 ActionPlan，不能直接修改系统状态

## 产出

- [ ] `models/execution.py` — ExecutionConfig + ToolInvocation
- [ ] `execution_mode.py` — 模式实现（DiscreteAsyncExecutor, BoundedMicroLoopExecutor）
- [ ] 修改 `simulation.py` — 根据配置选择执行器
- [ ] `test_execution_modes.py` — 两种模式的测试
- [ ] `test_tool_invocation.py` — 工具调用追踪测试

## 验收标准

- [ ] 离散异步模式下工具结果跨 tick 返回
- [ ] 有界微循环模式下循环次数受限制
- [ ] ToolInvocation 正确记录每次工具调用
- [ ] 预算限制生效
- [ ] 与现有测试兼容
