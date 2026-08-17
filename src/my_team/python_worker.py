"""Restricted Python execution worker (v0.8.0 P1-7).

Execution levels per SPEC §8.7「执行等级」:

  L0 python_compute   — pure computation: no filesystem, no network,
                        no subprocess. Restricted builtins + import
                        gate (allowlisted stdlib modules). JSON inputs
                        → structured `result` (JSON-validated).
  L1 python_transform — temp sandbox workspace: read-only input copies
                        + writable output dir. Artifacts are returned
                        via a manifest (path/hash/size/content) — they
                        enter the real workspace only through the
                        agent's own staged writes (base-hash checked).

HONEST CLASSIFICATION: both levels run in a host subprocess with
`-I` (isolated) mode, restricted builtins and an import gate — this
is CAPABILITY REDUCTION + PROCESS ISOLATION (accident prevention:
hallucinated code, wrong arguments, infinite loops). It is NOT a
security boundary: Python introspection (object.__subclasses__ and
friends) can in principle reach interpreter internals, and the
subprocess inherits the host user's filesystem view. Only L2
(isolated_python, SANDBOXED_PROCESS) with OS-level isolation is a
boundary — it does not exist yet.

The worker never runs user code in-process: everything is a
subprocess, killed by process group on timeout/cancel (no orphaned
children), bounded by max_runtime_ms and max_output_bytes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from my_team.sandbox_tools import run_sandboxed_process
from my_team.tool_protocol import canonical_json

# Default stdlib allowlist for L0/L1 (data processing focus). Each
# entry also admits its submodules (json.encoder etc.).
DEFAULT_ALLOWED_MODULES: tuple[str, ...] = (
    "json", "csv", "statistics", "math", "re", "datetime", "collections",
    "itertools", "string", "decimal", "fractions", "operator", "functools",
    "heapq", "bisect", "textwrap", "difflib", "uuid",
)

# Restricted builtins for L0: everything a pure-computation script
# needs; NOTHING that touches the environment.
_L0_BUILTINS = {
    "print", "len", "range", "sum", "min", "max", "abs", "round",
    "sorted", "str", "int", "float", "bool", "list", "dict", "tuple",
    "set", "frozenset", "zip", "enumerate", "isinstance", "issubclass",
    "any", "all", "reversed", "format", "chr", "ord", "bin", "hex",
    "oct", "divmod", "pow", "hash", "repr", "type", "getattr", "dir",
    "True", "False", "None", "NotImplemented", "Ellipsis", "map",
    "filter", "next", "iter", "slice", "complex", "bytes", "bytearray",
    "BaseException", "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "ZeroDivisionError", "OverflowError", "ArithmeticError",
    "RuntimeError", "NameError", "AttributeError", "StopIteration",
    "ValueError", "KeyboardInterrupt", "MemoryError",
}

# L1 adds filesystem access confined by CONVENTION to the sandbox
# workspace (input/ read-only, output/ writable).
_L1_BUILTINS = _L0_BUILTINS | {"open"}

_WRAPPER = r'''"""Restricted execution wrapper (written per run).

Run under `python -I` (isolated mode: no env vars, no site packages,
no PYTHONPATH, no user site). Arguments:

  --mode compute|transform
  --input-file <json>      inputs for the user code
  --result-file <json>     where the wrapper writes the structured result
  --code-file <py>         user code (read from file, not argv)
  --allow a,b,c            import allowlist (stdlib module names)
  --input-dir <dir>        L1: read-only input files
  --output-dir <dir>       L1: writable output dir (artifacts)

The user code runs with restricted globals: inputs, input_dir,
output_dir (L1), and `result` (must be JSON-serializable).
"""
import json
import sys


def _main() -> int:
    args = sys.argv[1:]
    opts: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and i + 1 < len(args):
            opts[a] = args[i + 1]
            i += 2
        else:
            i += 1
    mode = opts.get("--mode", "compute")
    allow = {m for m in opts.get("--allow", "").split(",") if m}

    with open(opts["--code-file"], "r", encoding="utf-8") as f:
        code = f.read()
    with open(opts["--input-file"], "r", encoding="utf-8") as f:
        inputs = json.load(f)

    real_import = __import__
    allowed = allow

    def _gate(name, globals=None, locals=None, fromlist=(), level=0):
        if name in allowed or any(name.startswith(a + ".") for a in allowed):
            return real_import(name, globals, locals, fromlist, level)
        raise ImportError(
            "module '%s' is not in the allowed set" % name
        )

    if mode == "compute":
        builtins_ = {}
        for k, v in vars(__builtins__).items():
            if k in _L0_BUILTINS_:
                builtins_[k] = v
        builtins_["__import__"] = _gate
        globals_ = {
            "__builtins__": builtins_,
            "__name__": "<sandbox>",
            "inputs": inputs,
            "result": None,
        }
    else:  # transform
        builtins_ = {}
        for k, v in vars(__builtins__).items():
            if k in _L1_BUILTINS_:
                builtins_[k] = v
        builtins_["__import__"] = _gate
        globals_ = {
            "__builtins__": builtins_,
            "__name__": "<sandbox>",
            "inputs": inputs,
            "input_dir": opts.get("--input-dir", ""),
            "output_dir": opts.get("--output-dir", ""),
            "result": None,
        }

    try:
        exec(compile(code, "<sandbox>", "exec"), globals_)  # noqa: S102
        result = globals_.get("result")
    except Exception as e:  # noqa: BLE001 — surfaced to the harness
        json.dump(
            {"sandbox_error": "%s: %s" % (type(e).__name__, e)},
            open(opts["--result-file"], "w", encoding="utf-8"),
        )
        return 2

    try:
        payload = json.dumps(result)
    except (TypeError, ValueError):
        json.dump(
            {"sandbox_error": "result must be JSON-serializable"},
            open(opts["--result-file"], "w", encoding="utf-8"),
        )
        return 3

    with open(opts["--result-file"], "w", encoding="utf-8") as f:
        f.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
'''


def _write_worker(directory: Path, l0_builtins: set[str], l1_builtins: set[str]) -> Path:
    """Materialize the wrapper with this module's builtin sets baked in.

    The builtin sets are injected as source so the wrapper does not
    import this module (isolated mode has no import path anyway).
    """
    path = directory / "worker.py"
    path.write_text(
        _WRAPPER.replace("_L0_BUILTINS_", repr(l0_builtins))
                .replace("_L1_BUILTINS_", repr(l1_builtins)),
        encoding="utf-8",
    )
    return path


def _run_worker(
    *,
    mode: str,
    inputs: dict[str, Any],
    code: str,
    allowed_modules: tuple[str, ...],
    timeout_ms: int,
    max_output_bytes: int,
    cwd: Path,
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
) -> dict[str, Any]:
    """Common runner: materialize wrapper + files, run, parse result."""
    code_path = cwd / "user_code.py"
    input_path = cwd / "inputs.json"
    result_path = cwd / "result.json"
    code_path.write_text(code, encoding="utf-8")
    input_path.write_text(canonical_json(inputs), encoding="utf-8")
    wrapper = _write_worker(cwd, _L0_BUILTINS, _L1_BUILTINS)

    cmd = [
        sys.executable, "-I", str(wrapper),
        "--mode", mode,
        "--input-file", str(input_path),
        "--result-file", str(result_path),
        "--code-file", str(code_path),
        "--allow", ",".join(allowed_modules),
    ]
    if input_dir is not None:
        cmd += ["--input-dir", str(input_dir)]
    if output_dir is not None:
        cmd += ["--output-dir", str(output_dir)]

    res = run_sandboxed_process(
        cmd,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        cwd=str(cwd),
        on_start=on_start,
        on_end=on_end,
    )

    result: dict[str, Any] = {}
    sandbox_error = ""
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            sandbox_error = "worker result file was not valid JSON"
        if isinstance(result, dict) and "sandbox_error" in result:
            sandbox_error = result["sandbox_error"]
            result = {}

    success = res["success"] and not sandbox_error and res["exit_code"] in {0, None}
    if res["timed_out"]:
        success = False
    out: dict[str, Any] = {
        "success": success,
        "result": result,
        "stdout": res["stdout"],
        "stderr": res["stderr"],
        "exit_code": res["exit_code"],
        "timed_out": res["timed_out"],
        "duration_ms": res["duration_ms"],
    }
    if sandbox_error:
        out["error"] = sandbox_error
    if not success and not sandbox_error:
        out["error"] = (
            "python worker failed"
            + (f" (exit {res['exit_code']})" if res["exit_code"] is not None else "")
            + (" [timed out]" if res["timed_out"] else "")
        )
    return out


def run_python_compute(
    code: str,
    inputs: dict[str, Any],
    allowed_modules: tuple[str, ...] = DEFAULT_ALLOWED_MODULES,
    timeout_ms: int = 10_000,
    max_output_bytes: int = 200_000,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
) -> dict[str, Any]:
    """L0: pure computation in a subprocess (no FS / network / child
    processes). Returns the ToolResult data dict."""
    with tempfile.TemporaryDirectory(prefix="pycompute-") as td:
        return _run_worker(
            mode="compute",
            inputs=inputs,
            code=code,
            allowed_modules=allowed_modules,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            cwd=Path(td),
            on_start=on_start,
            on_end=on_end,
        )


def run_python_transform(
    code: str,
    inputs: dict[str, Any],
    input_files: dict[str, str],
    allowed_modules: tuple[str, ...] = DEFAULT_ALLOWED_MODULES,
    timeout_ms: int = 30_000,
    max_output_bytes: int = 200_000,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
) -> dict[str, Any]:
    """L1: transform in a temp sandbox workspace.

    input_files: relpath → content, copied read-only into input/.
    Everything the user code writes to output/ comes back as an
    artifact manifest ({path, hash, size, content}); artifacts enter
    the real workspace only via the agent's staged writes.
    """
    with tempfile.TemporaryDirectory(prefix="pytransform-") as td:
        root = Path(td)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        for rel, content in input_files.items():
            target = (input_dir / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        res = _run_worker(
            mode="transform",
            inputs=inputs,
            code=code,
            allowed_modules=allowed_modules,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
            cwd=root,
            input_dir=input_dir,
            output_dir=output_dir,
            on_start=on_start,
            on_end=on_end,
        )

        artifacts: list[dict[str, Any]] = []
        for p in sorted(output_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(output_dir).as_posix()
            data = p.read_bytes()
            artifacts.append({
                "path": rel,
                "hash": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "content": data.decode("utf-8", errors="replace"),
            })
        res["artifacts"] = artifacts
        return res
