# v0.5.0 Implementation Plan — Deterministic Multi-Tick Loop

**Created:** 2026-08-17
**Status:** DONE (2026-08-17)
**Label:** v0.5.0 — Deterministic multi-tick runtime loop with staged-effect commit

## Goal

Prove the complete event-driven loop actually works end-to-end:

```text
Event wake → Agent activation → Decision → Tool execution
→ Pre-validation → Staged-effect commit → Effect application → Event publish
→ Next-tick Agent wake
```

## Completion Status

### P1: Completed

#### 1. Real Multi-Tick E2E ✅ (commit `1226316`)

`tests/test_e2e_multitick.py` — 10 tests using `ScriptedAgent` pattern:

- ✅ Runs `Simulation` through `run_tick()` across 3+ ticks
- ✅ 3 agent layers (Root → Research → WebResearch)
- ✅ Task creation, email round-trip, wake events
- ✅ File write via tool handler → committed to disk
- ✅ Idle agents not activated
- ✅ Audit log records activations
- ✅ Transaction buffer cleared after commit
- ⚠️ Used `ScriptedAgent` (deterministic plans) instead of `FakeLLMProvider` — the LLM decision path is NOT tested (documented in report)

#### 2. Unified Transaction Entry (SharedKB) ⚠️ PARTIAL

- ✅ `TransactionBuffer` wired into Simulation (commit `97174b6`)
- ✅ All 5 tool handlers stage effects through buffer
- ✅ `_phase_commit` validates + applies FILE_WRITE/EMAIL_SEND/TASK_CREATE
- ❌ `SharedKB.write()` direct path still exists (not renamed to `_apply_committed()`)
- ❌ No `stage_kb_write()` on ToolContext
- → **Deferred to v0.6.0**

#### 3. Implement `_phase_commit` Properly ✅ (commit `97174b6`)

- ✅ Validate effects (permission, lock, version checks)
- ✅ Resolve conflicts (deterministic by agent_id)
- ✅ Apply committed effects (FILE_WRITE, EMAIL_SEND, TASK_CREATE)
- ✅ Audit records TRANSACTION_COMMIT
- ⚠️ No cross-effect rollback — documented as Known Limitation #2
- ✅ Lock_token verification added (commit `7f14d9b`)

#### 4. Validate Phase: KB Version + Lock Token ✅ (commit `7f14d9b`)

- ✅ `check_lock` now verifies lock ownership AND lock_token (transaction.py + simulation.py)
- ✅ 10 lock integration tests in `test_lock_integration.py`:
  - Same-tick contention (holder wins, non-holder rejected)
  - Lock acquire → release → re-acquire across ticks
  - Expired lease → re-acquire
  - Write rejected without lock
  - Wrong/stale lock_token rejected
  - Private workspace exempt from locks
- ⚠️ KB version check exists in `check_version` but no E2E test yet

#### 5. Async LLM Semantics ⚠️ PARTIAL — architecture laid, flow not wired

- ✅ SPEC §8.6: Tick kernel ≠ ReAct logic (commit `614550a`)
- ✅ `models/intent.py`: 9 Intent types including SubmitLLMRequest/SubmitToolRequest
- ✅ `models/continuation.py`: AgentContinuation for resumable ReAct state
- ✅ `pending_ops.py`: PendingOperationRegistry (SUBMITTED → PENDING → COMPLETED/FAILED/CANCELLED/TIMED_OUT)
- ✅ `_phase_ingest()` collects completed ops and publishes wake events (commit `1fd1f74`)
- ✅ 12 tests for PendingOperationRegistry (commit `20618b2`)
- ❌ Agent `decide()` still returns ActionPlan, not Intents
- ❌ Continuation save/restore between ticks not wired
- ❌ No fake LLM provider for async E2E
- → **Deferred to v0.6.0** (models ready, wiring pending)

#### 6. Outbox Persistence ❌ NOT DONE

- Email queueing is in-process only
- No outbox status lifecycle (STAGED → COMMITTED → DISPATCHED)
- No idempotency, no crash recovery
- → **Deferred to v0.6.0**

### P2: Not Started

- **7. BoundedMicroLoop Re-Observation** ❌ — stale snapshot between rounds
- **8. AgentSnapshot Typed Views** ❌ — dict[str, Any] still used
- **9. Coverage Target 95%+** ⚠️ — 89.90% overall (llm_gateway 51%, llm_agent 61%)

### P3: Not Started

- **10. LLM Prompt Injection Defense** ❌
- **11. Crash Recovery (SQLite)** ❌
- **12. Performance Baseline** ❌

## Additional Work Delivered in v0.5.0

- **Consistency tests** (commit `0bc7442`): 10 tests in `test_consistency.py` — snapshot consistency, task+email consistency, commit behavior
- **Lock integration tests** (commit `7f14d9b`): 10 tests in `test_lock_integration.py`
- **Architectural redesign foundation** (commits `614550a`, `1fd1f74`): Intent/Continuation/PendingOperationRegistry models + 9-phase kernel tick cycle
- **SPEC updates**: §8.2 10-phase model, §8.6 concept separation, §9 agent lifecycle
- **Report**: honest v0.5.0 positioning, capability matrix, 11 known limitations

## Final State

```
547 passed in 0.87s
ruff: All checks passed!
mypy: Success: no issues found in 31 source files
Coverage: 89.90%
```
