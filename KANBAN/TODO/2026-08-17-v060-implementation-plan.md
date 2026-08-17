# v0.6.0 Implementation Plan — Async LLM + Continuation Runtime

**Created:** 2026-08-17
**Status:** TODO
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

### 1. Agent decide() Produces Intents

**Priority:** P1
**Acceptance:**
- `AgentRuntime.decide()` returns `list[Intent]` instead of `ActionPlan`
- LLMAgent: if continuation has pending LLM result → parse it; else → `SubmitLLMRequest`
- BaseAgent rule-based agents produce concrete Intents (DelegateIntent, SendEmailIntent, etc.)
- ActionPlan kept as internal parsing result, not the runtime interface

**Approach:**
- Extend `models/intent.py` with intent validation helpers
- Update `agent_runtime.py` protocol + BaseAgent/LLMAgent
- Keep backward-compatible `ActionPlan` for parsing layer only

### 2. Continuation Save/Restore Between Ticks

**Priority:** P1
**Acceptance:**
- After each activation, `AgentContinuation` is persisted in `AgentRuntimeState`
- At next activation, continuation is restored before `decide()`
- Pending LLM result delivered via continuation (`last_llm_result`)
- `react_turn`, `total_llm_calls`, `total_tool_calls` tracked correctly

### 3. Async LLM Flow E2E

**Priority:** P1
**Acceptance:**
- `FakeLLMProvider` returns deterministic responses after N ticks
- E2E: agent submits LLM request → WAITING_FOR_LLM → response arrives → agent re-activated → parses result → produces tool intent
- Timeout: LLM doesn't respond → op marked TIMED_OUT → agent wakes with error
- Test: stale response ignored (response for superseded request)

### 4. Async Tool Request Flow

**Priority:** P1
**Acceptance:**
- Tool calls go through PendingOperationRegistry (SubmitToolRequest)
- Tool execution happens outside tick (synchronous wrapper for local tools)
- TOOL_RESULT wake event → agent re-activated
- Tool timeout and retry

### 5. Complete Task Completion Roundtrip E2E

**Priority:** P1
**Acceptance:**
- Full scenario: Human request → Root → Research → WebResearch → Shared KB write → Research submits → Root completes task → Human receives reply
- Validates: task tree, emails, KB versions, audit log, agent states
- ≥2 parallel sub-tasks
- ≥1 lock contention resolved
- ≥1 failure + retry

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
 1. decide() → Intent (models + agent_runtime)
 2. Continuation save/restore (AgentRuntimeState)
 3. FakeLLMProvider + async LLM E2E
 4. Async tool requests
 5. Full task completion E2E
 6. Outbox persistence
 7. SharedKB single entry
 8. Commit rollback
 9. Pause semantics
10. LLM budget
11. SQLite persistence
```
