# P0-2: Pending op 注册纳入 tick 事务，回滚不留孤儿

**Phase:** v0.9 P0
**Source:** SPEC §3.3、§12.5；OI-003 P0-2
**Priority:** high

## 目标
Act 阶段注册的 pending op（LLM/tool）必须属于本 tick 事务。
Commit 回滚时，本 tick 注册的 op 与 Agent 状态/Continuation 的
变更一并撤销；任何 tick 结束后不得存在"未提交却已注册"的孤儿 op。

## 要求 / 规则
- 实现二选一：
  a) Act 只向 TransactionBuffer stage `PENDING_OP_REGISTER` effect，
     Commit 成功后再真正注册；或
  b) 维护本 tick 注册清单，`_rollback()` 中撤销 op 注册、
     `seen_requests` 历史、`AgentContinuation` 与 `AgentStateMachine`
     变更。
- 推荐 (a)：pending op 注册与其他 effect 同生命周期，后续统一
  Journal 也更容易。
- 回滚后 `_phase_publish` 不得 dispatch 本 tick 的 op。
- 被回滚的 request_id 不得污染 `seen_requests`（可复用）。

## 产出
- 事务化的 pending op 注册路径。
- 回归测试：同一 tick 内 `SubmitLLMRequest/SubmitToolRequest` +
  触发回滚的写操作 → 回滚后 pending_count 为 0、continuation 恢复
  FRESH/READY_TO_DECIDE、agent state 恢复、request_id 可复用。

## 验收标准
- [ ] 回滚 tick 后 `pending_ops.pending_count == 0`（本 tick 新增部分）
- [ ] 回滚后 agent 的 continuation 与 state 与 tick 开始前一致
- [ ] 同一 request_id 在回滚后可重新提交
- [ ] 连续 3 个回滚 tick 不累积 SUBMITTED op（复现 OI-003 P0-2 的实验转为测试）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
