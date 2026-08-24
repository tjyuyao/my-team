"""Sandboxed process execution for restricted tools (v0.7.0 P1-3 → T16a).

run_sandboxed_process runs a command with the restrictions available:
- list command only, never shell=True (no shell interpretation)
- hard wall-clock timeout; on expiry the WHOLE process group is killed
  (start_new_session=True + killpg) — no orphan children survive
- output truncated to max_output_bytes per stream (stdout/stderr)
- pinned working directory

Since v0.10-16a (T16a) a declarative SandboxConstraints spec
(sandbox_spec.py) can be attached; the host backend then enforces:

- resource limits (RLIMIT_CPU / RLIMIT_AS / RLIMIT_NPROC / RLIMIT_FSIZE)
  applied by a trusted shim before exec (no preexec_fn — thread-safe)
- environment sanitisation (pure env ops, platform independent):
  PYTHON* / exact-name / secret-keyword stripping, minimal PATH,
  pinned GIT_* + HOME
- network deny-by-default via unprivileged user + network namespaces
  (CLONE_NEWUSER|CLONE_NEWNET) — lowest-privilege route; unavailable
  hosts report the constraint as not applied (never silently)
- read-only bind mounts via user + mount namespaces
  (CLONE_NEWUSER + CLONE_NEWNS + MS_BIND/MS_REMOUNT|MS_RDONLY),
  reported per pair

Every enforced constraint is reported in ``sandbox_report``
(constraints declared → applied per constraint + notes). This is the
OI-001「可落地部分」: the manifest declares the spec, the backend
enforces it — declaration is the contract, enforcement is the boundary.

run_sandboxed_process with constraints=None keeps the exact pre-T16a
behavior (used by python_compute/python_transform, which remain
LOCAL_PROCESS).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from my_team.sandbox_spec import SandboxConstraints

_TRUNC_MARKER = "\n...[truncated {} bytes]"

# Workspace copy: dirs excluded from the sandbox snapshot (host-only
# state — git history, venv, caches, per-agent private workspace,
# harness scratch).
_WORKSPACE_COPY_EXCLUDE = frozenset({
    ".git", ".venv", ".uv-cache", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".coverage", "tmp", "private", ".claude",
})

# Env var carrying the shim's report file path (internal; never
# stripped by sanitisation — no PYTHON* prefix, no secret keyword).
_SANDBOX_REPORT_ENV = "_SANDBOX_REPORT"

# --- Linux namespace shim --------------------------------------------------
# A tiny trusted interpreter run BEFORE the real command: applies
# rlimits, unshares user/network/mount namespaces (lowest-privilege
# route — unprivileged user namespaces), applies read-only bind mounts,
# writes a JSON report, then execs the real command (same PID, so
# process-group kill/cancel keeps working). Avoids preexec_fn (not
# thread-safe) — the shim is a separate process, safe to spawn from any
# thread.
_SANDBOX_SHIM = r'''
import ctypes
import json
import os
import sys

try:
    import resource
except ImportError:  # non-POSIX
    resource = None

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
CLONE_NEWNET = 0x40000000
MS_BIND = 4096
MS_REMOUNT = 32
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_REC = 16384
MS_PRIVATE = 262144


def _report_path():
    return os.environ.get("_SANDBOX_REPORT")


def _write_report(constraints_applied, notes):
    path = _report_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"constraints_applied": constraints_applied, "notes": notes}, f)
    except OSError:
        pass


def _unshare(flags):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(flags) != 0:
        return False, "errno %d" % ctypes.get_errno()
    return True, ""


def _mountinfo_entry(mountpoint):
    """(source, fstype, options) of the mount at mountpoint, from
    /proc/self/mountinfo — the RO remount must replay the original
    mount's source/fstype/options (util-linux does the same)."""
    mp = os.path.realpath(mountpoint)
    try:
        lines = open("/proc/self/mountinfo", encoding="utf-8").read().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split(" ")
        if len(parts) > 5 and parts[4] == mp:
            rest = line.split(" - ", 1)
            if len(rest) != 2:
                return None
            fields = rest[1].split(" ", 2)
            if len(fields) != 3:
                return None
            return fields[1], fields[0], parts[5]
    return None


def main():
    args = sys.argv[1:]
    if "--" not in args:
        _write_report({}, ["shim: missing '--' separator"])
        return 2
    sep = args.index("--")
    opts, cmd = args[:sep], args[sep + 1:]
    if not cmd:
        _write_report({}, ["shim: empty command"])
        return 2

    limits = {}
    need_netns = False
    need_mountns = False
    binds = []
    i = 0
    while i < len(opts):
        o = opts[i]
        if o == "--limit" and i + 2 < len(opts):
            limits[opts[i + 1]] = int(opts[i + 2])
            i += 3
        elif o == "--netns":
            need_netns = True
            i += 1
        elif o == "--mountns":
            need_mountns = True
            i += 1
        elif o == "--bind" and i + 2 < len(opts):
            binds.append((opts[i + 1], opts[i + 2]))
            i += 3
        else:
            i += 1

    applied = {}
    notes = []

    # 1) resource limits (before any namespace work)
    for name, value in limits.items():
        if resource is None:
            applied["rlimit_" + name] = False
            notes.append("rlimit %s: resource module unavailable" % name)
            continue
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
            applied["rlimit_" + name] = True
        except (ValueError, OSError) as e:
            applied["rlimit_" + name] = False
            notes.append("rlimit %s: %s" % (name, e))

    # 2) user namespace (lowest-privilege route for net/mount isolation)
    need_user = need_netns or need_mountns or bool(binds)
    user_ok = False
    if need_user:
        try:
            ok, err = _unshare(CLONE_NEWUSER)
        except Exception as e:  # noqa: BLE001 — shim robustness
            ok, err = False, str(e)
        if not ok:
            notes.append("user namespace unavailable: %s" % err)
        else:
            try:
                uid, gid = os.getuid(), os.getgid()
                try:
                    open("/proc/self/setgroups", "w").write("deny")
                except (FileNotFoundError, PermissionError):
                    pass
                open("/proc/self/uid_map", "w").write("0 %d 1" % uid)
                open("/proc/self/gid_map", "w").write("0 %d 1" % gid)
                os.setgid(0)
                os.setuid(0)
                user_ok = True
            except Exception as e:  # noqa: BLE001 — shim robustness
                notes.append("user namespace mapping: %s" % e)

    # 3) network deny: fresh network namespace — loopback only, down
    if need_netns:
        ok, err = _unshare(CLONE_NEWNET)
        applied["netns"] = ok
        if not ok:
            notes.append("network namespace: %s" % err)

    # 4) mount namespace + read-only binds
    if need_mountns or binds:
        ok, err = _unshare(CLONE_NEWNS)
        applied["mountns"] = ok
        if not ok:
            notes.append("mount namespace: %s" % err)
        elif binds:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None)
            for src, dst in binds:
                key = "readonly_bind_" + src
                try:
                    os.makedirs(dst, exist_ok=True)
                    if libc.mount(src.encode(), dst.encode(), None, MS_BIND, None) != 0:
                        applied[key] = False
                        notes.append("bind %s: errno %d" % (src, ctypes.get_errno()))
                        continue
                    entry = _mountinfo_entry(dst)
                    if entry is None:
                        applied[key] = False
                        notes.append("bind %s: mountinfo entry missing" % src)
                        continue
                    source, fstype, mopts = entry
                    r = libc.mount(
                        source.encode(), dst.encode(), fstype.encode(),
                        MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV,
                        mopts.encode(),
                    )
                    applied[key] = r == 0
                    if r != 0:
                        notes.append("remount-ro %s: errno %d" % (src, ctypes.get_errno()))
                except OSError as e:
                    applied[key] = False
                    notes.append("bind %s: %s" % (src, e))

    _write_report(applied, notes)
    os.execvpe(cmd[0], cmd, os.environ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _shim_command(
    cmd: list[str], constraints: SandboxConstraints,
) -> list[str]:
    """Build the shim invocation that enforces ``constraints`` then
    execs ``cmd`` (same PID — process-group kill still works)."""
    args: list[str] = [sys.executable, "-c", _SANDBOX_SHIM]
    for name, value in (
        ("RLIMIT_CPU", constraints.cpu_seconds),
        ("RLIMIT_AS", constraints.memory_bytes),
        ("RLIMIT_NPROC", constraints.max_processes),
        ("RLIMIT_FSIZE", constraints.max_file_bytes),
    ):
        if value is not None:
            args += ["--limit", name, str(value)]
    if constraints.deny_network:
        args.append("--netns")
    if constraints.isolated_mount:
        args.append("--mountns")
    for src, dst in constraints.readonly_binds:
        args += ["--bind", src, dst]
    args += ["--"] + list(cmd)
    return args


def _truncate(text: str, max_bytes: int) -> str:
    if len(text) <= max_bytes:
        return text
    return text[:max_bytes] + _TRUNC_MARKER.format(len(text) - max_bytes)


def _read_sandbox_report(report_path: str | None) -> dict[str, Any] | None:
    if report_path is None or not os.path.exists(report_path):
        return None
    try:
        with open(report_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    applied = payload.get("constraints_applied")
    notes = payload.get("notes", [])
    return {
        "applied": applied if isinstance(applied, dict) else {},
        "notes": notes if isinstance(notes, list) else [],
    }


@contextmanager
def make_workspace_copy(
    workspace: str | Path | None = None,
    *,
    exclude: frozenset[str] = _WORKSPACE_COPY_EXCLUDE,
) -> Iterator[Path]:
    """Temp snapshot copy of the workspace — the sandbox's cwd.

    run_tests must never run against the host directory (T17
    by-product): pytest writes .pytest_cache / __pycache__ / tmp files
    that would pollute the host tree. The sandbox sees a throwaway
    copy; writes land there and die with the TemporaryDirectory.
    """
    root = Path(workspace or Path.cwd()).resolve()
    with tempfile.TemporaryDirectory(prefix="ws-sandbox-") as td:
        dst = Path(td) / "workspace"
        shutil.copytree(
            root, dst,
            ignore=shutil.ignore_patterns(*exclude),
            symlinks=False,
        )
        yield dst


def run_sandboxed_process(
    cmd: list[str],
    *,
    timeout_ms: int = 30_000,
    max_output_bytes: int = 200_000,
    cwd: str | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
    constraints: SandboxConstraints | None = None,
    env: dict[str, str] | None = None,
    home: str | None = None,
) -> dict[str, Any]:
    """Run cmd with timeout + output truncation, killing the process
    group on timeout. Returns a dict suitable for ToolResult.data.

    Result keys: success, timed_out, exit_code (None on timeout),
    stdout, stderr, duration_ms — plus sandbox_report when
    ``constraints`` is given ({constraints, applied, notes}).

    ``constraints`` (T16a): declarative isolation spec enforced by the
    host backend (rlimits + env sanitisation + netns + RO binds).
    ``env``: explicit environment (sanitised via constraints when
    given; used as-is otherwise). ``home``: HOME redirect target for
    pin_git_env.

    on_start / on_end: called with the Popen when it spawns and when
    it finishes — the simulation registers the live process per request
    so cancel_operation can physically kill it. on_end fires on every
    exit path (normal, timeout, external kill).
    """
    # MUST be a list: subprocess.Popen treats a str command with shell
    # semantics — that path is forbidden here.
    if (
        not isinstance(cmd, list)
        or not cmd
        or not all(isinstance(c, str) for c in cmd)
    ):
        raise ValueError("cmd must be a non-empty list of strings")

    run_env = env
    report_path: str | None = None
    shim_report: dict[str, Any] | None = None
    if constraints is not None:
        run_env = constraints.sanitized_env(
            dict(os.environ if env is None else env), home=home,
        )
        fd, report_path = tempfile.mkstemp(prefix="sandbox-report-")
        os.close(fd)
        run_env[_SANDBOX_REPORT_ENV] = report_path
        cmd = _shim_command(cmd, constraints)

    proc: subprocess.Popen | None = None
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=run_env,
            start_new_session=True,
        )
    except OSError as e:
        if report_path is not None:
            _cleanup_report(report_path)
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
        if report_path is not None:
            shim_report = _read_sandbox_report(report_path)
            _cleanup_report(report_path)

    duration_ms = int((time.monotonic() - started) * 1000)
    result: dict[str, Any] = {
        "success": not timed_out and proc.returncode == 0,
        "timed_out": timed_out,
        # On timeout the process was SIGKILLed by us (returncode -9) —
        # report exit_code=None: there is no natural exit.
        "exit_code": None if timed_out else proc.returncode,
        "stdout": _truncate(stdout or "", max_output_bytes),
        "stderr": _truncate(stderr or "", max_output_bytes),
        "duration_ms": duration_ms,
    }
    if constraints is not None:
        applied = dict((shim_report or {}).get("applied", {}))
        # Environment sanitisation is pure env ops performed by the
        # parent — always applied when constraints are given.
        applied.setdefault("env_sanitization", True)
        result["sandbox_report"] = {
            "constraints": constraints.describe(),
            "applied": applied,
            "notes": (shim_report or {}).get("notes", []),
        }
    return result


def _cleanup_report(report_path: str) -> None:
    try:
        os.unlink(report_path)
    except OSError:
        pass
