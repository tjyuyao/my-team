---
kind: task
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§15；KANBAN/PLAN/v0.8.0-plan（P2-7）
priority: medium
---

# v0.10-16a: v0.8 遗留 — run_tests 真实隔离（P2-7）

## 目标
`run_tests` 从同进程调用升级为真实隔离的执行等级
（SANDBOXED_PROCESS），收掉 v0.8.0 计划 P2-7。

## 要求 / 规则
- 只读挂载（临时工作区副本）、网络 deny-by-default、资源限制
  （CPU/内存/进程数/文件大小）、环境净化（sitecustomize/PYTHONPATH/
  PATH/secret 剥离）、GIT_* 固定。
- 达成后 `run_tests` 执行类升级为 `SANDBOXED_PROCESS`。

## 产出
- SANDBOXED_PROCESS 执行等级与 run_tests 升级。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **现状事实**：`ExecutionClass.SANDBOXED_PROCESS` 枚举已存在
  （tool_manifest.py），但 run_tests 实际声明 `LOCAL_PROCESS`；
  `sandbox_tools.run_sandboxed_process` 只有 timeout + 输出截断 + 进程组
  杀，只读挂载 / 网络 deny / 资源限制 / 环境净化均未提供（OI-001 可落地
  部分）。
- **平台相关（按决策 4 处理）**：内核定义 ExecutionClass 规范与约束声明，
  隔离后端可插拔；真实隔离后端仍需实现（mount/rlimit/cgroups 等）。
- **验证难**：测试需证明网络真被拒、环境真净化（非自证）。
- **并入「工具执行环境对齐」**：T17 by-product 的 run_tests/git cwd 宿主
  目录问题并入本卡（同一段代码，分开做必冲突）。

## 验收标准
- [ ] run_tests 在只读挂载 + 网络拒绝 + 资源限制下执行
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
