"""Sandboxed process execution for restricted tools (v0.7.0 P1-3).

run_sandboxed_process runs a command with the restrictions available
WITHOUT the full sandbox protocol (which is OPEN_ISSUE/OI-001 scope):
- list command only, never shell=True (no shell interpretation)
- hard wall-clock timeout; on expiry the WHOLE process group is killed
  (start_new_session=True + killpg) — no orphan children survive
- output truncated to max_output_bytes per stream (stdout/stderr)
- pinned working directory

NOT yet provided (OI-001): read-only mount, network deny-by-default,
resource limits, approval policy, sandbox worker process. Tools using
this helper declare execution_class=SANDBOXED_PROCESS and their
manifests state the enforced constraints.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Callable

_TRUNC_MARKER = "\n...[truncated {} bytes]"


def _truncate(text: str, max_bytes: int) -> str:
    if len(text) <= max_bytes:
        return text
    return text[:max_bytes] + _TRUNC_MARKER.format(len(text) - max_bytes)


def run_sandboxed_process(
    cmd: list[str],
    *,
    timeout_ms: int = 30_000,
    max_output_bytes: int = 200_000,
    cwd: str | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
) -> dict[str, Any]:
    """Run cmd with timeout + output truncation, killing the process
    group on timeout. Returns a dict suitable for ToolResult.data.

    Result keys: success, timed_out, exit_code (None on timeout),
    stdout, stderr, duration_ms.

    on_start / on_end (v0.8.0 P2-10): called with the Popen when it
    spawns and when it finishes — the simulation registers the live
    process per request so cancel_operation can physically kill it.
    on_end fires on every exit path (normal, timeout, external kill).
    """
    # MUST be a list: subprocess.Popen treats a str command with shell
    # semantics — that path is forbidden here.
    if (
        not isinstance(cmd, list)
        or not cmd
        or not all(isinstance(c, str) for c in cmd)
    ):
        raise ValueError("cmd must be a non-empty list of strings")

    proc: subprocess.Popen | None = None
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as e:
        return {
            "success": False,
            "timed_out": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"spawn failed: {e}",
            "duration_ms": 0,
        }
    if on_start is not None:
        on_start(proc)

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_ms / 1000.0)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Kill the ENTIRE process group (start_new_session=True made
        # the child a session leader) — no orphans survive.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()
    finally:
        if on_end is not None:
            on_end(proc)

    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "success": not timed_out and proc.returncode == 0,
        "timed_out": timed_out,
        # On timeout the process was SIGKILLed by us (returncode -9) —
        # report exit_code=None: there is no natural exit.
        "exit_code": None if timed_out else proc.returncode,
        "stdout": _truncate(stdout or "", max_output_bytes),
        "stderr": _truncate(stderr or "", max_output_bytes),
        "duration_ms": duration_ms,
    }
