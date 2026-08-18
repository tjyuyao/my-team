"""T19 crash-guard tests.

Verifies: repeated kernel-level crashes (full-tick rollbacks / uncaught
tick exceptions) auto-pause the system with reason='crash_guard' and
fire Provider/Owner emergency callbacks FIRST (then pause); business
failures (local FAILED, T18 分级) never count as crashes; the sliding
window ages out; resume re-arms the guard.

To force a kernel crash we stage a FILE_WRITE whose target path is a
DIRECTORY — apply raises IsADirectoryError (unexpected exception).
"""
from __future__ import annotations

import pytest

from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.reliability import CrashReport
from my_team.simulation import Simulation, SimulationConfig
from my_team.transaction import EffectType


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _make_sim(
    window: int = 10, threshold: int = 3,
) -> Simulation:
    return Simulation(
        agent_tree=_make_tree(),
        config=SimulationConfig(
            crash_guard_window_ticks=window,
            crash_guard_threshold=threshold,
        ),
    )


def _boom(sim: Simulation) -> None:
    """Stage a kernel-boom FILE_WRITE (target path occupied by a dir)."""
    from uuid import uuid4
    path = f"boom-{uuid4().hex[:8]}"
    home = sim._private_store.agent_home("agent.root")
    (home / path).mkdir(parents=True, exist_ok=True)
    sim._transaction_buffer.stage(
        EffectType.FILE_WRITE, "agent.root", path,
        data={"content": "boom"},
    )


def _stage_business_failure(sim: Simulation) -> None:
    """Stage a duplicate TASK_CREATE — deterministic local FAILED (T18),
    NOT a crash."""
    sim.task_tree.create(
        task_id="task.dup", title="T",
        creator_agent_id="agent.root", owner_agent_id="agent.root",
    )
    sim._transaction_buffer.stage(
        EffectType.TASK_CREATE, "agent.root", "task.dup",
        data={"task_id": "task.dup", "title": "Dup",
              "creator_agent_id": "agent.root",
              "owner_agent_id": "agent.root"},
    )


class TestCrashGuardTrigger:
    def test_repeated_crashes_pause_and_notify(self) -> None:
        """3 kernel crashes within the window → Provider/Owner callbacks
        fire and the system pauses with reason='crash_guard'."""
        sim = _make_sim(window=10, threshold=3)
        calls: list[tuple[str, CrashReport]] = []
        sim.crash_guard.register_emergency_callback(
            "provider", lambda r: calls.append(("provider", r)),
        )
        sim.crash_guard.register_emergency_callback(
            "owner", lambda r: calls.append(("owner", r)),
        )

        for tick in range(3):
            _boom(sim)
            sim.run_tick()
            sim._transaction_buffer.clear()

        # Guard triggered; system paused for a human
        assert sim.crash_guard.triggered
        assert sim.is_paused
        assert sim.pause_reason == "crash_guard"

        # Both recipients were notified, in order, with a full report
        assert [c[0] for c in calls] == ["provider", "owner"]
        report = calls[0][1]
        assert report.crash_count >= 3
        assert report.threshold == 3
        assert len(report.crash_ticks) == 3
        assert report.last_error
        assert report.tick == 2

        # Audited: 3 SYSTEM_CRASH + 1 CRASH_GUARD_TRIGGERED
        crashes = sim.audit_log.for_event_type(AuditEventType.SYSTEM_CRASH)
        assert len(crashes) == 3
        triggers = sim.audit_log.for_event_type(
            AuditEventType.CRASH_GUARD_TRIGGERED,
        )
        assert len(triggers) == 1

    def test_no_auto_resume_and_paused_ticks_refused(self) -> None:
        """Paused state persists; run_tick refuses to execute."""
        sim = _make_sim(window=10, threshold=3)
        for _ in range(3):
            _boom(sim)
            sim.run_tick()
            sim._transaction_buffer.clear()

        assert sim.is_paused
        with pytest.raises(RuntimeError, match="paused"):
            sim.run_tick()

    def test_resume_rearms_guard(self) -> None:
        """After a human resume, a renewed crash loop re-triggers."""
        sim = _make_sim(window=10, threshold=3)
        for _ in range(3):
            _boom(sim)
            sim.run_tick()
            sim._transaction_buffer.clear()
        assert sim.is_paused

        sim.resume()
        assert not sim.is_paused
        assert sim.pause_reason == ""
        assert not sim.crash_guard.triggered  # re-armed (window kept)

        # One more crash stays below threshold after re-arm (count is
        # below 3 again only if the window kept sliding — here the same
        # window still holds 3, so a single new crash re-triggers).
        _boom(sim)
        sim.run_tick()
        assert sim.is_paused
        assert sim.crash_guard.triggered


class TestBusinessFailureIsNotCrash:
    def test_local_failures_never_trigger_guard(self) -> None:
        """Even repeated deterministic failures (duplicate task_id) do
        NOT count as crashes — business failure is normal, not systemic."""
        sim = _make_sim(window=10, threshold=3)
        for _ in range(5):
            _stage_business_failure(sim)
            sim.run_tick()
            sim._transaction_buffer.clear()
            # Remove the dup task so the next tick can fail identically
            sim._task_tree._tasks.pop("task.dup", None)
            sim._task_tree._parent_map.pop("task.dup", None)

        assert not sim.crash_guard.triggered
        assert not sim.is_paused
        crashes = sim.audit_log.for_event_type(AuditEventType.SYSTEM_CRASH)
        assert len(crashes) == 0

    def test_stray_single_crash_below_threshold(self) -> None:
        """A single kernel crash (window 10, threshold 3) does not pause."""
        sim = _make_sim(window=10, threshold=3)
        _boom(sim)
        sim.run_tick()
        sim._transaction_buffer.clear()

        assert not sim.crash_guard.triggered
        assert not sim.is_paused
        assert sim.crash_guard.crash_ticks == [0]


class TestUncaughtTickException:
    def test_uncaught_exception_counts_as_crash(self) -> None:
        """run_tick raising uncaught is a crash event; the third one
        triggers the guard; the exception is re-raised to the caller."""
        sim = _make_sim(window=10, threshold=3)
        calls: list[CrashReport] = []
        sim.crash_guard.register_emergency_callback(
            "owner", lambda r: calls.append(r),
        )

        # Two kernel rollbacks…
        for _ in range(2):
            _boom(sim)
            sim.run_tick()
            sim._transaction_buffer.clear()

        # …then an uncaught exception inside the next tick
        def _boom_phase(*args, **kwargs):
            raise RuntimeError("uncaught phase boom")
        sim._phase_audit = _boom_phase  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="uncaught phase boom"):
            sim.run_tick()

        assert sim.is_paused
        assert sim.pause_reason == "crash_guard"
        assert len(calls) == 1
        assert calls[0].last_error == "uncaught phase boom"


class TestSlidingWindow:
    def test_window_slides_and_evicts_old_crashes(self) -> None:
        """Crashes older than the window age out — a late crash does not
        combine with them."""
        sim = _make_sim(window=4, threshold=3)
        assert sim.crash_guard.record_crash(1, "e1") is None
        assert sim.crash_guard.record_crash(2, "e2") is None
        # Ticks 3..6 healthy; at tick 7 the window holds only {7}
        assert sim.crash_guard.record_crash(7, "e3") is None
        assert not sim.crash_guard.triggered
        assert sim.crash_guard.crash_ticks == [7]

    def test_config_controls_window_and_threshold(self) -> None:
        sim = _make_sim(window=3, threshold=2)
        assert sim.crash_guard.window_ticks == 3
        assert sim.crash_guard.threshold == 2

        # 2 crashes within window 3 → trigger
        sim.crash_guard.record_crash(0, "e1")
        report = sim.crash_guard.record_crash(1, "e2")
        assert report is not None
        assert sim.crash_guard.triggered

    def test_unknown_recipient_rejected(self) -> None:
        sim = _make_sim()
        with pytest.raises(ValueError, match="recipient"):
            sim.crash_guard.register_emergency_callback(
                "nobody", lambda r: None,
            )
