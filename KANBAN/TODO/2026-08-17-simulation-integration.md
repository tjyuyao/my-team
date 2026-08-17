# Simulation 集成层

**Phase:** 6 - 系统集成
**Source:** SPEC §3, §8, §10
**Priority:** P0
**Review ref:** 差距 §8.1, §8.2

## 目标

将所有独立模块组合为完整可运行的 Simulation 类。

## 要求

```python
class Simulation:
    config: SimulationConfig
    agent_tree: AgentTree
    mail_system: MailSystem
    task_tree: TaskTree
    shared_kb: SharedKB
    tick_engine: TickEngine
    human_control: HumanControl
    audit_log: AuditLog
    runtimes: dict[str, AgentRuntime]

    def run(self, max_ticks: int) -> SimulationResult: ...
    def run_tick(self) -> TickResult: ...
```

- 从 JSON 配置文件初始化所有组件
- 将 AgentRuntime 注册到 TickEngine 的 Decide/Act 阶段
- 在 Deliver 阶段调用 MailSystem.deliver
- 在 Commit 阶段执行完整事务提交
- 在 Audit 阶段写入 AuditLog
- 集成 TimeoutChecker 在固定 phase 执行

## 产出

- `src/my_team/simulation.py`
- `tests/test_simulation.py`（端到端测试）

## 验收标准

- [ ] 能从配置文件创建 Simulation 实例
- [ ] 能运行至少 1 个 tick 不报错
- [ ] AgentRuntime 能在 Decide/Act 阶段被调用
- [ ] 邮件在 Deliver 阶段正确投递
- [ ] 端到端委派流程可执行
