"""SimulationRuntime — wall-clock tick loop with lifecycle control.

Wraps Simulation to provide:
- Real-time tick execution with configurable tick duration
- Start/pause/resume/step lifecycle control
- Tick duration changes applied at tick boundaries
- Thread-safe status queries

Usage:
    sim = Simulation(agent_tree=tree)
    runtime = SimulationRuntime(sim, tick_duration_seconds=0.1)
    runtime.start()       # background thread, wall-clock ticks
    # ... or ...
    runtime.step(5)       # synchronous, 5 ticks immediately
    runtime.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class SimulationRuntime:
    """Wall-clock tick loop with lifecycle control.

    Wraps a Simulation instance and provides start/stop/pause/resume/step
    controls. The runtime applies pending tick duration changes at each
    tick boundary.
    """

    def __init__(
        self,
        simulation: Any,
        tick_duration_seconds: float = 1.0,
    ) -> None:
        self._sim = simulation
        self._tick_duration = max(0.01, tick_duration_seconds)
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._start_time: float | None = None
        self._ticks_completed = 0

    @property
    def simulation(self) -> Any:
        return self._sim

    @property
    def tick_duration(self) -> float:
        with self._lock:
            return self._tick_duration

    def set_tick_duration(self, seconds: float) -> None:
        """Set tick duration. Takes effect from the next tick."""
        with self._lock:
            self._tick_duration = max(0.01, seconds)

    def start(self) -> None:
        """Start the wall-clock tick loop in a background thread.

        If already running, this is a no-op.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            self._start_time = time.monotonic()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="sim-runtime",
                daemon=True,
            )
            self._thread.start()
        logger.info("Runtime started (tick_duration=%.3fs)", self._tick_duration)

    def stop(self) -> None:
        """Stop the tick loop and wait for the current tick to finish."""
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info("Runtime stopped after %d ticks", self._ticks_completed)

    def pause(self) -> None:
        """Pause tick execution. Current tick completes before pausing."""
        self._sim.human_control.pause()

    def resume(self) -> None:
        """Resume tick execution."""
        self._sim.human_control.resume()

    def step(self, n: int = 1) -> list[Any]:
        """Execute n ticks synchronously (no background thread).

        Returns the list of TickResults.
        """
        results = []
        for _ in range(n):
            if self._sim._tick_engine.state.value == "paused":
                break
            self._sim._human_control.apply_pending_duration_changes()
            result = self._sim.run_tick()
            results.append(result)
            self._ticks_completed += 1
        return results

    @property
    def status(self) -> dict[str, Any]:
        """Current runtime status."""
        uptime = 0.0
        if self._start_time is not None:
            uptime = time.monotonic() - self._start_time
        return {
            "tick": self._sim._tick_engine.current_tick,
            "state": self._sim._tick_engine.state.value,
            "uptime_seconds": round(uptime, 2),
            "ticks_completed": self._ticks_completed,
            "tick_duration_seconds": self.tick_duration,
            "pending_ops_count": self._sim._pending_ops.count_in_flight(
                agent_id="",
            ) if hasattr(self._sim._pending_ops, "count_in_flight") else 0,
            "running": self._running,
        }

    def _run_loop(self) -> None:
        """Background tick loop with wall-clock pacing."""
        while self._running:
            # Check pause state
            if self._sim._tick_engine.state.value == "paused":
                time.sleep(0.1)
                continue

            # Apply pending duration changes at tick boundary
            self._sim._human_control.apply_pending_duration_changes()

            try:
                self._sim.run_tick()
                self._ticks_completed += 1
            except Exception:
                logger.exception("Tick %d failed", self._sim._tick_engine.current_tick)
                # Continue loop — transient errors shouldn't kill the runtime

            # Wall-clock sleep
            with self._lock:
                duration = self._tick_duration
            if duration > 0:
                time.sleep(duration)

    def __repr__(self) -> str:
        return (
            f"SimulationRuntime(tick={self._sim._tick_engine.current_tick}, "
            f"duration={self._tick_duration:.2f}s, "
            f"running={self._running})"
        )
