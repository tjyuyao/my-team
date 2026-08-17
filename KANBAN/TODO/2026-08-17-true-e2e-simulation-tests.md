# True E2E Simulation Tests

**Phase:** Testing
**Source:** review #26; report §7 P2; report §6
**Priority:** P2 — Verification

## 目标

编写真正的端到端测试，驱动 `Simulation.run()` 通过完整 7-phase tick cycle。

## 背景

当前 8 个 "E2E" 测试实际上是协议集成测试——直接实例化子系统并调用 API，没有经过 `Simulation.run()`。

## 要求

1. 测试使用 `Simulation.from_config_file()` 初始化
2. 测试调用 `Simulation.run(max_ticks=N)`
3. 测试验证完整的 tick 周期：Freeze → Deliver → Observe → Decide → Act → Commit → Audit
4. 测试验证 AgentRuntime.observe/decide/act 流水线
5. 测试验证邮件跨 tick 投递
6. 测试验证审计日志从完整 simulation 运行中生成
7. 测试覆盖多 Agent 协作场景

## 产出

- [ ] `test_simulation_run_e2e.py`
- [ ] 覆盖至少 3 种协作场景
- [ ] 验证审计日志完整性

## 验收标准

- [ ] 测试驱动 `Simulation.run()`
- [ ] 测试覆盖 7-phase tick cycle
- [ ] 测试验证 AgentRuntime 管道
- [ ] 测试验证邮件投递
- [ ] 测试验证审计日志
