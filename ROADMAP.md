# My-Team roadmap

## v0.14 — sandbox and trust

- [x] Replace legacy `data/devices` source area with device-owned homes; remove
  per-agent devices and `bound_agent`; add scoped device-maintainer unload → edit
  → reload lifecycle
- [x] Network denial, resource limits, source stamping, stale-channel rejection
- [x] Bash semantics and verification probes
- [x] Focused `tests/` suite (14 tests, all passing)
- [x] Acceptance gate, milestone report, and plan archive

## v0.15 — executable core

- [ ] External event → Agent → injected tool → Device → result → Journal
- [ ] Small deterministic Agent decision loop
- [ ] Persisted Authority registrations, grants, and installs
- [ ] Restart/bootstrap tests

## v0.16+ — governance and extension

- [ ] Journal query capability with read scopes
- [ ] HumanTask, approval, and escalation
- [ ] Task dependencies and derived-task references
- [ ] Static validation for device declarations/packages
- [ ] Scenario packages after generic semantics close

## Testing contract

Keep the default suite small. Each test must cover a distinct invariant or failure mode;
prefer end-to-end vectors over method-by-method duplication. Legacy and competitor tests
are reference material, not CI.
