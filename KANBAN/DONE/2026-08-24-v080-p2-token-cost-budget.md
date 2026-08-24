---
kind: task
status: completed
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§15；KANBAN/PLAN/v0.8.0-plan（P2-11）
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
- [x] 超过 token/cost 预算的 LLM 请求在 PreValidate 被拒绝（拒整个回合，
      审计 `budget.rejected`；concurrency 同路径，审计沿用
      `permission.denied`/`llm_budget_exceeded`）
- [x] `uv run pytest -q` 全绿（1006 passed：基线 934 + 本卡 25 + 并行卡
      ~47）；`ruff`/`mypy` 通过；kanban_lint 0

## 实现注记（2026-08-24，T16c 完成）
- **定价表（`src/my_team/budget.py`）**：`DEFAULT_PRICING_PER_1M` =
  模型 → (输入 $/1M, 输出 $/1M)，参考主流供应商常见定价（OpenAI
  gpt-4o/4o-mini/4/3.5-turbo、Anthropic claude-opus-4/sonnet-4/
  3-5-sonnet/3-opus/3-haiku、Gemini 1.5/2.0、ollama 免费）；未知模型
  回退保守中间价 $1/$2（绝不静默免费）；`BudgetConfig.pricing` 可覆盖。
  `LLMGateway.complete()` 现用定价表填充 `LLMInvocation.cost`。
- **拒绝语义 = 拒整个回合**（按主 agent 决策落地）：`_phase_validate`
  对含 LLM 请求的回合先做预算预扫描——「累计 + 本次请求估算」任一
  维度超限（cost 优先，token/request_count/wall_time 次之）或并发超限
  （in-flight + 本回合请求数），整个回合全部 intent 判
  `BUDGET_EXCEEDED` 失败：不注册 op、不执行、不 commit（事务原子性，
  无「部分 LLM 调用发生」中间态）。
- **累计器**：`BudgetTracker` 按 agent / task / simulation 三作用域分列
  `request_count / input/output token / cost / wall_time_seconds`；只对
  已交付（completed）的调用记账，超时/失败/取消不计；实际用量取
  provider usage，缺失时回退请求级保守估算。
- **配置字段**：`SimulationConfig.budget: BudgetConfig`（agent/task/
  simulation 三组 `BudgetLimits`，0=不限；agent concurrency=0 时回退
  `max_concurrent_llm_requests`，显式 0 保持旧 force-denial 语义）。
- **持久化组件**：`_collect_state["budget"]` / `_restore_state` →
  `BudgetTracker.snapshot()/restore()`（与 human_pending_actions 同模式；
  旧存档无该键时干净加载），模拟重启累计不丢，重启后拒绝语义照常生效。
- **测试 `tests/test_budget.py`（25 个）**：定价/估算/累计/snapshot-
  restore 单元；limit check 单元（cost 优先、各作用域、并发回退）；集成
  ——PreValidate 真拒（LLM 请求未执行、回合未 commit、审计有记录）、
  累计后第二轮被拒、save/load 重启后累计仍在且继续拒、并发整回合拒、
  gateway cost 填充。既有测试无回归。
- 全量验证：`uv run pytest -q` 1006 passed；`ruff check src tests` 与
  `mypy src` 通过；kanban_lint 0 violation。
