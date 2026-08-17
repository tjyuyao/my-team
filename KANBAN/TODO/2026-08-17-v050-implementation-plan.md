# v0.5.0 Implementation Plan — Reliable Closed-Loop

**Created:** 2026-08-17
**Status:** TODO
**Label:** v0.5.0 — Reliable Closed-Loop with Async LLM

## Goal

Prove the complete event-driven loop actually works end-to-end:

```text
Event wake → Agent activation → LLM decision → Tool execution
→ Pre-validation → Transaction commit →副作用投递 → Event publish
→ Next-tick Agent wake
```

## P1: Must Complete

### 1. Real Multi-Tick E2E with Fake LLM

**Priority:** P1
**Acceptance:** A deterministic fake-LLM scenario that:
- Runs `Simulation.from_config_file(...)` → `simulation.run(...)`
- Spans ≥5 ticks
- Has ≥3 agent layers (Root → Manager → Worker)
- Has ≥2 parallel sub-tasks
- Has ≥1 shared KB write
- Has ≥1 lock contention
- Has ≥1 failure + retry
- Validates: task tree, emails, KB, audit log all correct
- Can reconstruct event order from audit log

**Approach:**
- Create `FakeLLMProfile` that returns deterministic ActionPlans from a scripted sequence
- Script: Human request → Root delegates → Research accepts + delegates → Worker executes tool → writes KB → returns result → Research aggregates → Root summarizes
- Test in `tests/test_e2e_multitick.py`

### 2. Unified Transaction Entry (SharedKB)

**Priority:** P1
**Acceptance:**
- `SharedKB.write()` is no longer callable directly by agents
- All writes go through `TransactionBuffer.stage()` → `Commit()` pipeline
- Direct write method renamed to `_apply_committed()` (internal)
- Validate phase can check KB version and lock token

**Approach:**
- Rename `SharedKB.write()` → `SharedKB._apply_committed()`
- Add `stage_kb_write()` to ActionContext or ToolContext
- Update all callers to use staged path

### 3. Implement `_phase_commit` Properly

**Priority:** P1
**Acceptance:**
- `_phase_commit` actually commits staged effects atomically
- Task state updates are committed
- KB writes are committed
- Lock operations are committed
- Email queueing is committed
- On commit failure, all effects are rolled back
- Audit records the commit result

**Approach:**
- Iterate through `all_results` for each agent
- Group effects by type (task_update, kb_write, lock_op, email)
- Apply atomically via subsystem APIs
- On any failure, rollback committed effects

### 4. Validate Phase: KB Version + Lock Token

**Priority:** P1
**Acceptance:**
- KB write actions validate version number matches current version
- KB write actions validate lock is held and lock_token matches
- Lock acquisition validates resource is not locked by another agent
- Missing/wrong version → validation failure → action rejected

### 5. Async LLM Semantics

**Priority:** P1
**Acceptance:**
- LLM call returns a pending result, not blocking
- Agent transitions to `WAITING_FOR_LLM`
- LLM response arrives as `TOOL_RESULT`-like event in later tick
- Agent re-activated when response arrives
- `max_llm_calls_per_activation` is a real scheduling constraint
- Timeout if LLM doesn't respond within N ticks

**Approach:**
- Option A (simpler): LLM call happens synchronously within Act, but we document the blocking boundary and add timeout
- Option B (full async): LLM request queued, agent waits, response arrives as event
- **Recommend Option A for v0.5.0**, Option B for v0.6.0

### 6. Outbox Persistence + Idempotent Delivery

**Priority:** P1
**Acceptance:**
- Outbox entries have: effect_id, idempotency_key, status, retry_count, last_error
- Status lifecycle: STAGED → COMMITTED → DISPATCHING → DISPATCHED / FAILED
- Failed dispatches retry up to N times
- Idempotency key prevents duplicate delivery
- Process can recover outbox state on restart

## P2: Should Complete

### 7. BoundedMicroLoop Re-Observation

**Priority:** P2
**Acceptance:**
- Between LLM→Tool→LLM rounds, agent gets a fresh partial snapshot
- At minimum: updated tool results, updated lock states
- Full snapshot too expensive; use delta update

### 8. AgentSnapshot Typed Views

**Priority:** P2
**Acceptance:**
- Replace `dict[str, Any]` in AgentSnapshot with typed views:
  - `EmailView(email_id, thread_id, sender, subject, type, task_id, body)`
  - `TaskView(task_id, status, title, owner)`
  - `SharedResourceView(path, version, content)`
  - `LockView(resource, owner, lease_until, lock_token)`
- Each agent only sees fields authorized by its role

### 9. Coverage Target: 95%+

**Priority:** P2
**Acceptance:**
- `llm_gateway.py` ≥ 80%
- `llm_agent.py` ≥ 80%
- `executors.py` ≥ 85%
- `simulation.py` ≥ 85%
- Overall ≥ 92%

## P3: Nice to Have

### 10. LLM Prompt Injection Defense

**Priority:** P3
**Acceptance:**
- System prompt has clear policy layer
- Untrusted content (emails, KB, web results) is clearly demarcated
- Model output is re-validated by system before execution
- No agent can escalate privileges via text

### 11. Crash Recovery (SQLite)

**Priority:** P3
**Acceptance:**
- All simulation state persisted to SQLite
- Can pause → shutdown → restart → resume
- Test: commit-before-crash, commit-after-crash, outbox-recovery

### 12. Performance Baseline

**Priority:** P3
**Acceptance:**
- Measure: 7/50/500 agents
- Report: activations/tick, LLM concurrency, memory, tick duration
