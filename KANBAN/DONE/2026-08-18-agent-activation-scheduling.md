---
kind: task
status: completed
phase: Core Runtime
source: SPEC §8.4, §9.2, §9.3; tick semantics discussion
priority: high
---

# Agent Activation & Scheduling System


## 目标

实现事件驱动的 Agent 调度系统，使 Agent 只在有待处理工作时才被唤醒执行。

## 背景

当前 `Simulation.run()` 每个 tick 无条件调用所有 Agent 的 observe/decide/act，导致：
- 空闲 Agent 浪费 LLM 调用成本
- 无意义的状态变化和审计记录
- Agent 竞争共享锁
- 模拟时间与计算资源耦合

SPEC §8.4 和 §9.2-9.3 定义了事件驱动激活模型。

## 要求

### 数据模型

1. **AgentActivation** — 记录一次激活周期
   - activation_id, agent_id, request_id, task_ids
   - triggered_by (事件类型), started_at_tick
   - max_llm_calls, max_tool_calls

2. **WakeupEvent** — 触发 Agent 唤醒的事件
   - event_id, agent_id, event_type
   - source_id, available_at_tick

3. **WakeCondition** — Agent 的唤醒条件
   - event_types: frozenset[str]
   - wake_at_tick: int | None
   - task_ids: frozenset[str]
   - resources: frozenset[str]

### Agent 状态机

4. 实现 §9.1 定义的完整状态机：
   - idle → ready → observing → deciding → acting
   - acting → waiting_for_tool / waiting_for_child / waiting_for_mail / waiting_for_lock / idle
   - blocked, paused, failed, terminated

5. 状态转换规则：
   - idle → ready: 唤醒事件到达
   - ready → observing: 调度器分配 activation
   - observing → deciding: observe 完成
   - deciding → acting: decide 完成
   - acting → waiting_*: 提交异步操作
   - waiting_* → ready: 事件到达（工具结果、邮件、子任务完成等）
   - 任意 → paused: 系统暂停
   - 任意 → failed: 不可恢复错误

### 调度器

6. **AgentScheduler** 类：
   - 维护 ready queue
   - 每个 tick 只调度满足唤醒条件的 Agent
   - 创建 AgentActivation 记录
   - 管理 activation 预算（max_llm_calls, max_tool_calls）

7. **唤醒事件生成**：
   - 邮件到达 → 生成 wakeup event
   - 工具结果返回 → 生成 wakeup event
   - 子任务状态变化 → 生成 wakeup event
   - 锁释放 → 生成 wakeup event
   - 定时器到期 → 生成 wakeup event

### Simulation 集成

8. 修改 `Simulation.run_tick()`：
   - Phase 3 (Observe): 只对 ready 状态的 Agent 执行
   - Phase 4 (Decide): 只对 observing 状态的 Agent 执行
   - Phase 5 (Act): 只对 deciding 状态的 Agent 执行
   - Phase 6 (Commit): 更新 Agent 状态机

## 产出

- [ ] `scheduler.py` — AgentScheduler + WakeupEvent + WakeCondition
- [ ] `models/activation.py` — AgentActivation 数据模型
- [ ] `agent_state_machine.py` — Agent 状态机实现
- [ ] 修改 `simulation.py` — 集成调度器
- [ ] `test_scheduler.py` — 调度器单元测试
- [ ] `test_agent_state_machine.py` — 状态机测试
- [ ] `test_activation_integration.py` — 集成测试

## 验收标准

- [ ] IDLE Agent 不调用 LLM
- [ ] 只有待处理事件的 Agent 被调度
- [ ] 状态机转换正确
- [ ] 唤醒事件正确生成和消费
- [ ] 调度器预算限制生效
- [ ] 与现有 410 测试兼容
