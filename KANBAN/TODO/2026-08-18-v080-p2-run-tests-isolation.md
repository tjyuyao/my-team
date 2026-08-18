---
kind: task
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§14；KANBAN/PLAN/v0.8.0-plan（P2-7）
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

## 验收标准
- [ ] run_tests 在只读挂载 + 网络拒绝 + 资源限制下执行
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
