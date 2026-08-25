"""Declarative sandbox constraint spec (v0.10-16a / T16a).

The kernel defines WHAT isolation a SANDBOXED_PROCESS tool requires as
a declarative constraint spec; a pluggable backend enforces it. This
module holds the spec (SandboxConstraints) and the backend interface
(SandboxBackend Protocol); the concrete host backend lives in
sandbox_tools.py. Per decision 4 (platform dependence): the kernel owns
the ExecutionClass semantics and the constraint declaration, the
isolation backend is pluggable — and the manifest declaring
SANDBOXED_PROCESS without constraints is invalid (declaration is the
contract; the backend's enforcement is the boundary, OI-001).

Environment sanitisation is pure env manipulation (platform
independent, POSIX + Windows both safe): PYTHON* stripping (the
sitecustomize / PYTHONPATH vector), exact-name stripping, secret-keyword
stripping, minimal PATH and pinned GIT_* / HOME.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

# Env var name prefixes always stripped: PYTHON* is the interpreter
# injection vector (sitecustomize via PYTHONPATH, user site, startup
# hooks). `-I` isolated mode is applied separately at spawn time.
STRIP_ENV_PREFIXES: tuple[str, ...] = ("PYTHON",)

# Secret-name keywords: any env var whose normalized (lowercased,
# underscore-removed) name contains one of these is stripped.
DEFAULT_SECRET_KEYWORDS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "credential", "apikey",
    "auth",
)

# Minimal deterministic PATH for sandboxed runs (no user dirs, no
# host-specific entries). Commands are always invoked by absolute
# interpreter path / `-m`, so nothing beyond system bins is needed.
MINIMAL_PATH = "/usr/bin:/bin"

# GIT_* vars pinned/unset for deterministic, host-config-free runs
# (OI-001: git 类命令固定 cwd 与 GIT_DIR/GIT_WORK_TREE/HOME/
# GIT_CONFIG_NOSYSTEM).
_GIT_UNSET = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


@dataclass(frozen=True)
class SandboxConstraints:
    """Declarative isolation spec for a SANDBOXED_PROCESS run.

    Each field is a DECLARED constraint; the enforcement backend
    applies it and reports per-constraint success/failure in the run
    result (``sandbox_report``). Resource limits map to POSIX rlimits
    (RLIMIT_CPU / RLIMIT_AS / RLIMIT_NPROC / RLIMIT_FSIZE); network
    deny and read-only binds are implemented with user+network/mount
    namespaces (``unshare``) on Linux — lowest-privilege route
    (unprivileged user namespaces), unavailable environments report the
    constraint as not applied (deny-by-default reporting, never silent).
    """

    # -- resource limits ---------------------------------------------------
    cpu_seconds: int | None = None
    # ^ RLIMIT_CPU: max CPU seconds (soft=hard). Exceeding → SIGXCPU/SIGKILL.
    memory_bytes: int | None = None
    # ^ RLIMIT_AS: max virtual address space (soft=hard). Exceeding → MemoryError.
    max_processes: int | None = None
    # ^ RLIMIT_NPROC: max processes for the real UID (soft=hard). NOTE:
    #   counts user-wide, not per-tree; a value below the user's live
    #   process count makes ANY fork fail (EAGAIN). A cgroup backend
    #   would scope it per-run; this is the POSIX approximation.
    max_file_bytes: int | None = None
    # ^ RLIMIT_FSIZE: max file size a child may write. Exceeding → SIGXFSZ / EFBIG.

    # -- environment sanitisation -----------------------------------------
    strip_env: tuple[str, ...] = ()
    # ^ Exact env var names to remove (e.g. "PYTHONPATH"). PYTHON*
    #   prefixes are always stripped regardless.
    strip_env_keywords: tuple[str, ...] = ()
    # ^ Secret-name keywords (normalized substring match on the var
    #   name); empty = no keyword stripping. Use DEFAULT_SECRET_KEYWORDS.
    minimal_path: bool = False
    # ^ Replace PATH with MINIMAL_PATH (no host-specific/user dirs).
    pin_git_env: bool = False
    # ^ Pin GIT_* to deterministic values (GIT_CONFIG_NOSYSTEM=1,
    #   GIT_TERMINAL_PROMPT=0, GIT_DIR/GIT_WORK_TREE/... unset) and
    #   redirect HOME to the sandbox home (host ~/.gitconfig etc.
    #   invisible).

    # -- OS-level isolation (namespace-backed, best effort) ----------------
    deny_network: bool = False
    # ^ New network namespace (CLONE_NEWNET via unprivileged user
    #   namespace): the child sees ONLY an empty loopback — no host
    #   interfaces, no routes. Requires user namespaces; EPERM hosts
    #   report netns as not applied.
    isolated_mount: bool = False
    # ^ New mount namespace (CLONE_NEWNS): mounts made by the sandbox
    #   never reach the host tree.
    readonly_binds: tuple[tuple[str, str], ...] = ()
    # ^ (host_src, sandbox_dst) bind mounts applied READ-ONLY inside
    #   the sandbox (MS_BIND + MS_REMOUNT|MS_RDONLY). Requires user +
    #   mount namespaces; applied per-pair, each reported.

    # -- derived helpers ---------------------------------------------------

    def __post_init__(self) -> None:
        for name, value in (
            ("cpu_seconds", self.cpu_seconds),
            ("memory_bytes", self.memory_bytes),
            ("max_processes", self.max_processes),
            ("max_file_bytes", self.max_file_bytes),
        ):
            if value is not None and value < 0:
                raise ValueError(f"sandbox constraint {name} must be >= 0")
        for pair in self.readonly_binds:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(p, str) and p for p in pair)
            ):
                raise ValueError(
                    f"readonly_binds entries must be (host_src, sandbox_dst) "
                    f"string pairs, got {pair!r}"
                )

    def describe(self) -> dict[str, Any]:
        """Canonical declarative description (manifest/report/audit)."""
        return {
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "max_processes": self.max_processes,
            "max_file_bytes": self.max_file_bytes,
            "strip_env": list(self.strip_env),
            "strip_env_keywords": list(self.strip_env_keywords),
            "minimal_path": self.minimal_path,
            "pin_git_env": self.pin_git_env,
            "deny_network": self.deny_network,
            "isolated_mount": self.isolated_mount,
            "readonly_binds": [list(p) for p in self.readonly_binds],
        }

    def sanitized_env(
        self,
        base: dict[str, str] | None = None,
        *,
        home: str | None = None,
    ) -> dict[str, str]:
        """Pure environment sanitisation (platform independent).

        Applies, in order: PYTHON* prefix stripping, exact-name
        stripping, secret-keyword stripping, minimal PATH, GIT_* pinning
        (+ HOME redirect when ``pin_git_env`` and ``home`` given).
        """
        env = dict(os.environ if base is None else base)

        for key in list(env):
            if key.startswith(STRIP_ENV_PREFIXES):
                env.pop(key, None)
        for key in self.strip_env:
            env.pop(key, None)
        keywords = tuple(k.lower().replace("_", "") for k in self.strip_env_keywords)
        if keywords:
            for key in list(env):
                norm = key.lower().replace("_", "")
                if any(kw in norm for kw in keywords):
                    env.pop(key, None)
        if self.minimal_path:
            env["PATH"] = MINIMAL_PATH
        if self.pin_git_env:
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_TERMINAL_PROMPT"] = "0"
            for key in _GIT_UNSET:
                env.pop(key, None)
            if home is not None:
                env["HOME"] = home
        return env


def pinned_git_env(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Pinned GIT_* environment for git tools running on the host repo.

    Keeps cwd/host repo (git needs the real .git) but removes host
    config influence and prompts: GIT_CONFIG_NOSYSTEM=1,
    GIT_TERMINAL_PROMPT=0, GIT_DIR/GIT_WORK_TREE/... unset.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    for key in _GIT_UNSET:
        env.pop(key, None)
    return env


class SandboxBackend(Protocol):
    """Pluggable isolation backend interface (decision 4).

    A backend consumes the declarative spec and runs the command with
    as much of it enforced as the platform allows; the result must
    carry ``sandbox_report`` (declared constraints → applied per
    constraint + notes). The host backend is sandbox_tools.
    """

    name: str

    def run(
        self,
        cmd: Sequence[str],
        *,
        constraints: SandboxConstraints | None,
        timeout_ms: int,
        max_output_bytes: int,
        cwd: str | None,
        env: dict[str, str] | None,
        home: str | None,
        on_start: Any,
        on_end: Any,
    ) -> dict[str, Any]: ...


__all__ = [
    "DEFAULT_SECRET_KEYWORDS",
    "MINIMAL_PATH",
    "SandboxBackend",
    "SandboxConstraints",
    "STRIP_ENV_PREFIXES",
    "pinned_git_env",
]
