# v0.6.0 Implementation Plan — Async LLM + Continuation Runtime

**Created:** 2026-08-17
**Status:** TODO (P1-1, P1-3, P1-5 done)
**Label:** v0.6.0 — Async LLM runtime with continuation-based agents

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

### 4. Async Tool Request Flow ⚠️ PARTIAL

- ✅ SubmitToolRequest → PendingOperationRegistry (remote tools)
- ✅ Local tools (read/ls) execute synchronously
- ❌ TOOL_RESULT wake event → re-activation flow not E2E tested
- ❌ Tool timeout and retry

### 5. Complete Task Completion Roundtrip E2E ✅ DONE (commit `43c5ce4`)

- ✅ Human request → Root async LLM → delegate → Research async LLM → write report + result email → Root completes task → human reply
- ✅ Validates: task tree (COMPLETED), emails, report file, agent states, audit log
- ❌ Shared KB write not yet in roundtrip (uses private file)
- ❌ ≥2 parallel sub-tasks not tested
- ❌ Lock contention in E2E not tested
- ❌ Failure + retry not tested

## P2: Should Complete

### 6. Outbox Persistence + Idempotent Delivery

**Priority:** P2
**Acceptance:**
- Outbox entries: effect_id, idempotency_key, status, retry_count, last_error
- Lifecycle: STAGED → COMMITTED → DISPATCHING → DISPATCHED / FAILED
- Failed dispatches retry up to N times
- Idempotency key prevents duplicate delivery

### 7. SharedKB Single Write Entry

**Priority:** P2
**Acceptance:**
- `SharedKB.write()` → `SharedKB._apply_committed()` (internal)
- `stage_kb_write()` as only public write API
- KB write E2E: permission → lock → version → commit → version increment

### 8. Cross-Effect Commit Rollback

**Priority:** P2
**Acceptance:**
- If TASK_CREATE succeeds but EMAIL_SEND fails, rollback task
- If FILE_WRITE fails, rollback other committed effects in same tick
- `_phase_commit` collects rollback actions during application

## P3: Nice to Have

### 9. Pause at Commit Boundary

**Priority:** P3
**Acceptance:**
- pause request → effective at next commit boundary
- No new activations scheduled
- External ops continue, results enter quarantine

### 10. LLM Budget + Rate Limiting

**Priority:** P3
**Acceptance:**
- Per-agent max concurrent LLM requests
- Provider 429/5xx handling with retry
- Token/cost budget per agent, task, simulation

### 11. Persistence (SQLite)

**Priority:** P3
**Acceptance:**
- Save/load: simulation, agents, tasks, emails, wake events, transactions, outbox, KB versions, locks, audit, LLM invocations
- Crash recovery: pause → shutdown → restart → resume

## Migration Order

```
 1. ✅ decide() → Intent (models + agent_runtime)          — e3af699
 2. ✅ Continuation save/restore (AgentRuntimeState)       — e3af699, a1e91fa
 3. ✅ FakeLLMProvider + async LLM E2E                     — a1e91fa
 4. ⏳ Async tool requests                                  — next
 5. ✅ Full task completion E2E                            — 43c5ce4
 6. Outbox persistence
 7. SharedKB single entry
 8. Commit rollback
 9. Pause semantics
10. LLM budget
11. SQLite persistence
```
