---
kind: task
status: completed
phase: Testing
source: "review #26; report §7 P2; report §6; SPEC §8.3-8.5"
priority: medium
---

# True E2E Simulation Tests


## 目标

编写真正的端到端测试，驱动 `Simulation.run()` 通过完整 7-phase tick cycle，验证 SPEC §8.3-8.5 定义的 tick 语义和激活模型。

## 背景

当前 8 个 "E2E" 测试实际上是协议集成测试——直接实例化子系统并调用 API，没有经过 `Simulation.run()`。

SPEC §8.3 定义 tick 为逻辑时间单位（非 API/LLM 调用），§8.4 定义 Agent 激活模型，§8.5 定义执行模式。

## 要求

### 基础 E2E 测试

1. 测试使用 `Simulation.from_config_file()` 初始化
2. 测试调用 `Simulation.run(max_ticks=N)`
3. 测试验证完整的 tick 周期：Freeze → Deliver → Observe → Decide → Act → Commit → Audit
4. 测试验证 AgentRuntime.observe/decide/act 流水线
5. 测试验证邮件跨 tick 投递（tick t → t+1 语义）
6. 测试验证审计日志从完整 simulation 运行中生成

### Tick 语义验证（§8.3）

7. 验证一次用户请求跨越多个 tick
8. 验证 tick ≠ API 请求 ≠ LLM 调用 ≠ 工具调用
9. 验证 Agent 完整响应跨越多个 tick

### 激活模型验证（§8.4）

10. 验证 IDLE Agent 不被调度
11. 验证只有有待处理事件的 Agent 被激活
12. 验证每次 activation 只执行一次 observe/decide/act
13. 验证 activation 预算限制

### 多 Agent 协作场景

14. 场景 1：Root → Research → WebResearch 三级委派
15. 场景 2：并行委派（Root → Research + Planning）
16. 场景 3：任务超时和取消
17. 场景 4：共享 KB 锁竞争

### 审计完整性

18. 验证每个 tick 的审计事件完整
19. 验证邮件发送/接收/投递都有审计记录
20. 验证工具调用有审计记录

## 产出

- [ ] `test_simulation_run_e2e.py`
- [ ] 覆盖至少 4 种协作场景
- [ ] 验证审计日志完整性
- [ ] 验证 tick 语义（§8.3）
- [ ] 验证激活模型（§8.4）

## 验收标准

- [ ] 测试驱动 `Simulation.run()`
- [ ] 测试覆盖 7-phase tick cycle
- [ ] 测试验证 AgentRuntime 管道
- [ ] 测试验证邮件投递
- [ ] 测试验证审计日志
- [ ] 测试验证 tick 语义
- [ ] 测试验证激活模型
