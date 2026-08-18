---
kind: task
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§14；KANBAN/PLAN/v0.8.0-plan（P2-11）
priority: medium
---

# v0.10-16c: v0.8 遗留 — token/cost 预算（P2-11）

## 目标
LLM 用量可度量、可限额，超限在 PreValidate 拒绝，收掉 v0.8.0
计划 P2-11。

## 要求 / 规则
- 定价表 + 每 agent/task/simulation 上限，PreValidate 拒绝；
  concurrency / request_count / token / cost / wall_time 分列。

## 产出
- 预算模型与 PreValidate 集成。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **计量已有半套**：llm.py 响应已带 input_tokens/output_tokens/cost；
  ContextCompiler 有 token budget。缺：定价表、每 agent/task/simulation
  累计器、PreValidate 集成（PreValidate 已存在，集成点清晰）。
- **跨 tick 持久化**：预算记账须纳入 `_collect_state`/load（模拟重启不丢
  累计）。
- **拒绝语义待定（开工时定）**：PreValidate 拒绝是拒单个 LLM 请求还是拒
  整个回合。

## 验收标准
- [ ] 超过 token/cost 预算的 LLM 请求在 PreValidate 被拒绝
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
