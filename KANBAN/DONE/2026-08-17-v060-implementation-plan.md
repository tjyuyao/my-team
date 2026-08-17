# v0.6.0 Implementation Plan — Async LLM + Continuation Runtime

**Created:** 2026-08-17
**Status:** DONE (P1 + P2 + P3-9/10/11 + post-review hardening)
**Label:** v0.6.0 — Async LLM/tool runtime prototype with continuation-based agents

## Goal

Complete the architectural redesign from v0.5.0:

```text
Agent produces Intent (non-blocking)
→ System registers PendingOperation
→ Agent → WAITING_FOR_LLM / WAITING_FOR_TOOL
→ External result arrives → WakeEvent
→ Agent re-activated → resumes continuation
```

ReAct is the agent's behavioral protocol; Tick is the kernel's advancement protocol. They are not 1:1.

## P1: Must Complete

### 1. Agent decide() Produces Intents ✅ DONE (commit `e3af699`)

- ✅ `decide_intents()` in AgentRuntime protocol + BaseAgent (ActionPlan → Intent conversion)
- ✅ LLMAgent: pending result → parse; else → `SubmitLLMRequest` (never blocks)
- ✅ ActionPlan kept as internal parsing result
- ✅ 12 tests in `test_intent_pipeline.py`

### 2. Continuation Save/Restore Between Ticks ✅ DONE (part of `e3af699`, `a1e91fa`)

- ✅ `AgentContinuation` in `AgentRuntimeState`, passed to `decide_intents()`
- ✅ LLM result delivered via continuation (`receive_llm_result` in `_phase_ingest`)
- ✅ `react_turn`, `total_llm_calls`, `total_tool_calls` tracked
- ✅ `finalize_result_processing()` resets phase after result consumed

### 3. Async LLM Flow E2E ✅ DONE (commit `a1e91fa`)

- ✅ `FakeLLMProvider` — deterministic scripted responses with latency
- ✅ E2E: submit → WAITING_FOR_LLM → response → re-activation → parse → tool intent
- ✅ Timeout: op marked TIMED_OUT
- ✅ 6 tests in `test_e2e_async_llm.py`
- ⚠️ Stale response rejection not yet tested

### 4. Async Tool Request Flow ✅ DONE (commit `db6f9c2`)

- ✅ SubmitToolRequest → PendingOperationRegistry (remote tools)
- ✅ FakeToolExecutor completes TOOL_REQUEST ops with scripted results
- ✅ TOOL_RESULT wake event → agent re-activated → acts on result
- ✅ Tool timeout (TIMED_OUT) and error result delivery
- ✅ Hybrid LLM → tool → result multi-hop continuation
- ✅ 5 tests in `test_e2e_async_tool.py`

### 5. Complete Task Completion Roundtrip E2E ✅ DONE (commit `43c5ce4`)

- ✅ Human request → Root async LLM → delegate → Research async LLM → write report + result email → Root completes task → human reply
- ✅ Validates: task tree (COMPLETED), emails, report file, agent states, audit log
- ❌ Shared KB write not yet in roundtrip (uses private file)
- ❌ ≥2 parallel sub-tasks not tested
- ❌ Lock contention in E2E not tested
- ❌ Failure + retry not tested

## P2: Should Complete

### 6. Outbox Persistence + Idempotent Delivery ✅ DONE (commit `3b4798c`)

- ✅ Outbox entries: entry_id, idempotency_key, effect_id, status,
  attempt_count, last_error, next_retry_tick
- ✅ Lifecycle: STAGED → COMMITTED → DISPATCHING → DISPATCHED / FAILED / DEAD
- ✅ Failed dispatches retried up to max_retries, then DEAD
- ✅ Idempotency key prevents duplicate staging
- ✅ Simulation integration: EMAIL_SEND → outbox → MailSystem
- ✅ 7 tests in `test_outbox.py`
- ⚠️ Not yet persisted to disk (in-memory; SQLite persistence is P3-11)

### 7. SharedKB Single Write Entry ✅ DONE (commit `8e6b386`)

- ✅ `SharedKB.write()` → `SharedKB._apply_committed()` (internal)
- ✅ `handle_kb_write` tool handler stages KB_WRITE effects
- ✅ KB E2E: permission → lock → version → commit → version increment
- ✅ Fixed latent bug: LockManager `__len__` made `lock_manager or LockManager()` replace the empty manager — SharedKB and Simulation used DIFFERENT lock managers
- ✅ 8 tests in `test_kb_e2e.py`

### 8. Cross-Effect Commit Rollback ✅ DONE (commit `79209cd`)

- ✅ TASK_CREATE failure rolls back staged EMAIL_SEND
- ✅ Rollback removes created tasks and emails (reverse order)
- ✅ TRANSACTION_ROLLBACK audit event recorded
- ✅ Prior tick state preserved
- ✅ 4 tests in `test_commit_rollback.py`

## P3: Nice to Have

### 9. Pause at Commit Boundary ✅ DONE (commit `7e99368`)

**Priority:** P3
**Acceptance:**
- ✅ pause request → effective at next commit boundary
- ✅ No new activations scheduled
- ✅ External ops continue, results enter quarantine
- ✅ 4 tests in `test_pause_async.py`

### 10. LLM Budget + Rate Limiting ⚠️ PARTIAL

**Priority:** P3
**Acceptance:**
- ✅ Per-agent max concurrent LLM requests (`max_concurrent_llm_requests`,
  enforced in Phase 6 Validate; tested in `test_op_hardening.py`)
- ❌ Provider 429/5xx handling with retry — retry is agent-driven after
  timeout errors; provider-level backoff not implemented
- ❌ Token/cost budget per agent, task, simulation

### 11. Persistence (SQLite) ✅ DONE (commit pending)

**Priority:** P3
**Acceptance:**
- ✅ Save/load: config, agent tree, tick engine (tick + paused state),
  state epoch, tasks, emails (all + pending + per-mailbox), scheduler
  (wake conditions, queued events, activation history), outbox entries,
  pending operations, shared KB (resources + versions + permissions),
  locks, audit log, file-ops audit, agent runtime states
  (state machine + continuation) — `Simulation.save_to()` /
  `Simulation.load_from()` via `persistence.py` (SQLite)
- ✅ Crash recovery: pause → save → shutdown → load → resume, with
  quarantined external results delivered after resume
- ✅ Atomic saves: one SQLite transaction (all-or-nothing)
- ✅ Schema versioning + corruption/mismatch → clean failure
- ✅ 11 tests in `test_persistence.py` (roundtrip, lockstep
  determinism, quarantine recovery, atomicity, load errors)

---

## Post-Review Hardening (2026-08-17, applied to v0.6.0)

Per the v0.6.0 review (P0 + P1). Report, SPEC §8.6, and KANBAN updated.

| Item | Status |
|------|--------|
| 10-phase cycle with Act restored | ✅ — phase tracking + 4 tests (`test_phase_semantics.py`) |
| Frozen snapshot read view (read/ls) | ✅ — per-agent file view at Freeze; 2 tests |
| FILE_WRITE rollback (content restore) | ✅ — 2 tests (`test_snapshot_views.py`) |
| Shared KB rollback (content + version) | ✅ — 1 test (`test_commit_rollback.py`) |
| state_epoch + stale response fencing | ✅ — epoch mismatch + superseded; 3 tests (`test_epoch_fencing.py`) |
| Rollback increments state epoch | ✅ |
| Timeout → wake with structured error | ✅ — agent decides retry/fail/escalate; 2 tests |
| Timeout retry creates NEW request_id | ✅ |
| Duplicate request_id rejected | ✅ — intra-plan + cross-tick |
| Pending op cannot escape agent scope | ✅ |
| Cancelled op late result discarded | ✅ — `complete()` never resurrects terminal ops |
| LLM budget enforced in Validate | ✅ — per-agent max concurrent |
| Rolled-back email → no wake events | ✅ — 1 test |

**Remaining (v0.7.0):** provider 429/5xx retry, token/cost budget,
ToolManifest/OperationPolicy + tool contract, sandboxed tool
execution, typed AgentSnapshot views, BoundedMicroLoop re-observation.

## Migration Order

```
 1. ✅ decide() → Intent (models + agent_runtime)          — e3af699
 2. ✅ Continuation save/restore (AgentRuntimeState)       — e3af699, a1e91fa
 3. ✅ FakeLLMProvider + async LLM E2E                     — a1e91fa
 4. ✅ Async tool requests                                 — db6f9c2
 5. ✅ Full task completion E2E                            — 43c5ce4
 6. ✅ Outbox persistence + idempotent delivery            — 3b4798c
 7. ✅ SharedKB single entry                               — 8e6b386
 8. ✅ Commit rollback                                     — 79209cd
 9. ✅ Pause at commit boundary                            — 7e99368
10. ✅ LLM budget (concurrency)                            — post-review hardening
11. ✅ SQLite persistence + crash recovery                 — P3-11 (persistence.py)
```
