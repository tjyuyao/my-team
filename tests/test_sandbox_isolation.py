"""T16a: run_tests real isolation (SANDBOXED_PROCESS) — non-self-verifying
proofs.

Every proof makes the CHILD attempt the real operation and observes the
failure:

- environment sanitisation: a subprocess asserts PYTHON*/secret vars
  are stripped, PATH is minimal, GIT_* is pinned; a marker-based
  sitecustomize test proves sitecustomize is NOT effective (control:
  without sanitisation it fires)
- resource limits: memory allocation above RLIMIT_AS → MemoryError;
  file write above RLIMIT_FSIZE → EFBIG; fork under RLIMIT_NPROC=1 →
  EAGAIN; busy loop under RLIMIT_CPU → killed (control: same
  operations succeed without limits)
- network deny-by-default: inside the sandbox's fresh network namespace
  even loopback is unreachable and no host interface is visible
  (control: host loopback works)
- read-only bind mounts: a bind-mounted host dir rejects writes with
  EROFS while reads work (control: same dir writable when not mounted)

Namespace-backed constraints (netns / RO binds) are skipped with a note
on hosts where unprivileged user namespaces are unavailable — the
backend still reports them as not applied (never silently).

Tool-level integration: run_tests executes pytest in a TEMP WORKSPACE
COPY (host tree untouched), under the manifest's declared constraints
(sandbox_report present; relative test paths resolve inside the copy).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from my_team.agent_runtime import ToolContext
from my_team.agent_tree import AgentTree
from my_team.sandbox_spec import (
    DEFAULT_SECRET_KEYWORDS,
    MINIMAL_PATH,
    SandboxConstraints,
)
from my_team.sandbox_tools import run_sandboxed_process
from my_team.simulation import Simulation
from my_team.tool_manifest import (
    ExecutionClass,
    ToolManifest,
    ToolManifestError,
    builtin_manifests,
)

try:
    import resource  # noqa: F401  (POSIX-only)
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False

skip_no_resource = pytest.mark.skipif(
    not _HAS_RESOURCE, reason="resource module is POSIX-only",
)


def _make_tree(tools: list[str]) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": tools,
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _ctx(sim: Simulation, tool: str) -> ToolContext:
    return ToolContext(
        agent_id="agent.root", tick=0,
        allowed_tools=sim._tool_registry.get_allowed_tools("agent.root"),
    )


class TestEnvSanitization:
    """Pure env ops — platform independent, non-self-verifying."""

    def test_secrets_pythonpath_path_git_stripped(self, tmp_path) -> None:
        base = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": "/evil/site",
            "PYTHONHOME": "/evil/home",
            "MY_TEAM_SECRET": "s3cr3t",
            "GITHUB_TOKEN": "ghp_x",
            "OPENAI_API_KEY": "sk-x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "HOME": "/home/user",
        }
        code = (
            "import os\n"
            "print('PYTHONPATH' in os.environ)\n"
            "print('PYTHONHOME' in os.environ)\n"
            "print('MY_TEAM_SECRET' in os.environ)\n"
            "print('GITHUB_TOKEN' in os.environ)\n"
            "print('AWS_SECRET_ACCESS_KEY' in os.environ)\n"
            "print('PATH=' + os.environ.get('PATH', ''))\n"
            "print('GIT_CONFIG_NOSYSTEM=' + os.environ.get('GIT_CONFIG_NOSYSTEM', ''))\n"
            "print('GIT_TERMINAL_PROMPT=' + os.environ.get('GIT_TERMINAL_PROMPT', ''))\n"
            "print('GIT_DIR' in os.environ)\n"
            "print('HOME=' + os.environ.get('HOME', ''))\n"
        )
        constraints = SandboxConstraints(
            strip_env=("MY_TEAM_SECRET",),
            strip_env_keywords=DEFAULT_SECRET_KEYWORDS,
            minimal_path=True,
            pin_git_env=True,
        )
        res = run_sandboxed_process(
            [sys.executable, "-c", code],
            env=base,
            constraints=constraints,
            home="/sandbox-home",
            cwd=str(tmp_path),
        )
        assert res["success"], res["stderr"]
        lines = res["stdout"].splitlines()
        assert lines == [
            "False",          # PYTHONPATH
            "False",          # PYTHONHOME
            "False",          # MY_TEAM_SECRET (exact strip)
            "False",          # GITHUB_TOKEN (keyword)
            "False",          # AWS_SECRET_ACCESS_KEY (keyword)
            "PATH=" + MINIMAL_PATH,
            "GIT_CONFIG_NOSYSTEM=1",
            "GIT_TERMINAL_PROMPT=0",
            "False",          # GIT_DIR unset
            "HOME=/sandbox-home",
        ], res["stdout"]
        # The report declares the constraints; env sanitisation applied.
        report = res["sandbox_report"]
        assert report["constraints"]["minimal_path"] is True
        assert report["applied"].get("env_sanitization") is True

    def test_sitecustomize_not_effective(self, tmp_path) -> None:
        """A sitecustomize.py on PYTHONPATH must NOT load in the sandbox.

        Control first: WITHOUT sanitisation the same sitecustomize fires
        (marker created) — proving the setup is real; then the sanitized
        run must leave the marker absent.
        """
        sc_dir = tmp_path / "evil-site"
        sc_dir.mkdir()
        marker = tmp_path / "sitecustomize-ran.txt"
        (sc_dir / "sitecustomize.py").write_text(
            "open(%r, 'w').write('ran')\n" % str(marker),
            encoding="utf-8",
        )
        base = dict(os.environ)
        base["PYTHONPATH"] = str(sc_dir)
        base["PYTHONSTARTUP"] = str(sc_dir / "sitecustomize.py")

        control = run_sandboxed_process(
            [sys.executable, "-c", "print('child-ok')"],
            env=base, cwd=str(tmp_path),
        )
        assert "child-ok" in control["stdout"]
        assert marker.exists(), (
            "control: sitecustomize should have run without sanitisation"
        )
        marker.unlink()

        sanitized = run_sandboxed_process(
            [sys.executable, "-c", "print('child-ok')"],
            env=base, constraints=SandboxConstraints(), cwd=str(tmp_path),
        )
        assert "child-ok" in sanitized["stdout"]
        assert not marker.exists(), (
            "sitecustomize must not be effective in the sandbox"
        )


@skip_no_resource
class TestResourceLimits:
    """rlimits enforced in the child: the real operation fails."""

    def test_memory_limit_kills_allocation(self, tmp_path) -> None:
        code = "x = bytearray(1 << 29); print('alloc-ok')"  # 512 MiB
        # control: 64 MiB alloc without a limit succeeds
        ctl = run_sandboxed_process(
            [sys.executable, "-c", "x = bytearray(1 << 26); print('alloc-ok')"],
            cwd=str(tmp_path),
        )
        assert ctl["success"] and "alloc-ok" in ctl["stdout"]
        # sandboxed: RLIMIT_AS=64 MiB → 512 MiB alloc fails
        res = run_sandboxed_process(
            [sys.executable, "-c", code],
            constraints=SandboxConstraints(memory_bytes=64 * 1024 * 1024),
            cwd=str(tmp_path),
        )
        assert not res["success"], res["stdout"]
        assert "MemoryError" in res["stderr"], res["stderr"]
        assert res["sandbox_report"]["applied"].get("rlimit_RLIMIT_AS") is True

    def test_file_size_limit_blocks_write(self, tmp_path) -> None:
        code = (
            "open('big.bin', 'wb').write(b'x' * 100_000); print('write-ok')"
        )
        ctl = run_sandboxed_process(
            [sys.executable, "-c", code], cwd=str(tmp_path),
        )
        assert ctl["success"] and "write-ok" in ctl["stdout"]
        res = run_sandboxed_process(
            [sys.executable, "-c", code],
            constraints=SandboxConstraints(max_file_bytes=1024),
            cwd=str(tmp_path),
        )
        assert not res["success"]
        assert "too large" in res["stderr"].lower(), res["stderr"]
        assert res["sandbox_report"]["applied"].get("rlimit_RLIMIT_FSIZE") is True

    def test_process_limit_blocks_fork(self, tmp_path) -> None:
        code = (
            "import os\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os._exit(0)\n"
            "os.waitpid(pid, 0)\n"
            "print('fork-ok')\n"
        )
        ctl = run_sandboxed_process(
            [sys.executable, "-c", code], cwd=str(tmp_path),
        )
        assert ctl["success"] and "fork-ok" in ctl["stdout"]
        res = run_sandboxed_process(
            [sys.executable, "-c", code],
            constraints=SandboxConstraints(max_processes=1),
            cwd=str(tmp_path),
        )
        assert not res["success"]
        assert "temporarily unavailable" in res["stderr"].lower(), res["stderr"]
        assert res["sandbox_report"]["applied"].get("rlimit_RLIMIT_NPROC") is True

    def test_cpu_limit_kills_process(self, tmp_path) -> None:
        """RLIMIT_CPU=1s: a busy loop is killed (SIGXCPU then SIGKILL)."""
        res = run_sandboxed_process(
            [sys.executable, "-c", "while True: pass"],
            constraints=SandboxConstraints(cpu_seconds=1),
            cwd=str(tmp_path),
        )
        assert not res["success"]
        assert not res["timed_out"]  # killed by the LIMIT, not the timeout
        assert res["exit_code"] in (-24, -9), res
        assert res["sandbox_report"]["applied"].get("rlimit_RLIMIT_CPU") is True


class TestNetworkDeny:
    """Fresh network namespace: loopback only, and even that is down."""

    def test_network_denied_by_default(self, tmp_path) -> None:
        # control: the HOST loopback works (test env is sane)
        ctl_code = (
            "import socket\n"
            "s = socket.socket()\n"
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "s.bind(('127.0.0.1', 0))\n"
            "port = s.getsockname()[1]\n"
            "s.listen(1)\n"
            "c = socket.create_connection(('127.0.0.1', port), timeout=3)\n"
            "print('LOOPBACK_OK')\n"
        )
        ctl = run_sandboxed_process(
            [sys.executable, "-c", ctl_code], cwd=str(tmp_path),
        )
        assert "LOOPBACK_OK" in ctl["stdout"], ctl["stdout"]

        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 1), timeout=2)\n"
            "    print('LOOPBACK_CONNECTED')\n"
            "except OSError as e:\n"
            "    print('LOOPBACK_DENIED', e.errno)\n"
            "print('IFACES', sorted(n for _, n in socket.if_nameindex()))\n"
        )
        res = run_sandboxed_process(
            [sys.executable, "-c", code],
            constraints=SandboxConstraints(deny_network=True),
            cwd=str(tmp_path),
        )
        report = res["sandbox_report"]
        if not report["applied"].get("netns"):
            pytest.skip(
                "user+network namespaces unavailable on this host "
                "(unprivileged userns disabled)"
            )
        assert "LOOPBACK_DENIED" in res["stdout"], res["stdout"]
        # no host interface visible — only the empty loopback
        assert "IFACES ['lo']" in res["stdout"], res["stdout"]


class TestReadOnlyBinds:
    """Host dir bind-mounted READ-ONLY inside the sandbox (EROFS)."""

    def test_readonly_bind_denies_write(self, tmp_path) -> None:
        src = tmp_path / "host-src"
        src.mkdir()
        (src / "f.txt").write_text("secret", encoding="utf-8")
        cwd = tmp_path / "sandbox-cwd"
        cwd.mkdir()
        dst = str(cwd / "ro")

        code = (
            "import os\n"
            "os.makedirs('ro', exist_ok=True)\n"
            "try:\n"
            "    open('ro/new.txt', 'w').write('x')\n"
            "    print('WRITE_OK')\n"
            "except OSError as e:\n"
            "    print('WRITE_DENIED', e.errno)\n"
            "print('READ', open('ro/f.txt').read())\n"
        )
        # control: the same dir is writable when not RO-mounted
        ctl = run_sandboxed_process(
            [sys.executable, "-c", code], cwd=str(cwd),
        )
        assert "WRITE_OK" in ctl["stdout"], ctl["stdout"]

        res = run_sandboxed_process(
            [sys.executable, "-c", code],
            constraints=SandboxConstraints(
                readonly_binds=((str(src), dst),),
                isolated_mount=True,
            ),
            cwd=str(cwd),
        )
        report = res["sandbox_report"]
        if not report["applied"].get("readonly_bind_" + str(src)):
            pytest.skip(
                "user+mount namespaces unavailable on this host — "
                "read-only bind mounts not enforceable"
            )
        assert "WRITE_DENIED" in res["stdout"], res["stdout"]
        assert "READ secret" in res["stdout"], res["stdout"]


class TestRunTestsSandboxedTool:
    """run_tests executes pytest in a temp workspace copy under the
    manifest's declared constraints."""

    def _sim(self) -> Simulation:
        return Simulation(agent_tree=_make_tree(["run_tests"]))

    def test_runs_in_temp_workspace_copy(self, tmp_path) -> None:
        """cwd is a throwaway copy: relative writes land there, never
        in the host workspace (T17 by-product)."""
        host = Path.cwd()
        leak = host / "sandbox_leak.txt"
        leak.unlink(missing_ok=True)
        try:
            test_file = tmp_path / "test_pollute.py"
            test_file.write_text(
                "import pathlib\n"
                "def test_writes_relative():\n"
                "    pathlib.Path('sandbox_leak.txt').write_text('boom')\n"
                "    assert pathlib.Path('sandbox_leak.txt').read_text() == 'boom'\n",
                encoding="utf-8",
            )
            sim = self._sim()
            result = sim._tool_registry.execute(
                _ctx(sim, "run_tests"), "run_tests",
                test_path=str(test_file),
            )
            assert result.success, result.error
            assert result.data["exit_code"] == 0
            assert "1 passed" in result.data["stdout"]
            assert not leak.exists(), (
                "test wrote into the HOST workspace — cwd must be a "
                "temp workspace copy"
            )
            report = result.data["sandbox_report"]
            assert report["constraints"]["deny_network"] is True
            assert report["constraints"]["minimal_path"] is True
            assert report["constraints"]["pin_git_env"] is True
            assert report["applied"].get("env_sanitization") is True
            assert report["applied"].get("rlimit_RLIMIT_AS") is True
        finally:
            leak.unlink(missing_ok=True)

    def test_env_sanitized_and_limits_enforced_inside(self, tmp_path) -> None:
        """A real pytest inside the sandbox observes the sanitized env
        and the declared rlimits (values taken from the manifest)."""
        manifest = self._sim()._tool_registry.get_manifest("run_tests")
        assert manifest is not None
        c = manifest.sandbox_constraints
        assert c is not None
        test_file = tmp_path / "test_sandbox_checks.py"
        test_file.write_text(
            "import os\nimport resource\n\n"
            "def test_env_sanitized():\n"
            "    assert 'PYTHONPATH' not in os.environ\n"
            "    assert 'PYTHONHOME' not in os.environ\n"
            "    assert os.environ.get('PATH') == '/usr/bin:/bin'\n"
            "    assert os.environ.get('GIT_CONFIG_NOSYSTEM') == '1'\n"
            "    assert os.environ.get('GIT_TERMINAL_PROMPT') == '0'\n"
            "    assert 'GIT_DIR' not in os.environ\n\n"
            "def test_limits_applied():\n"
            f"    assert resource.getrlimit(resource.RLIMIT_AS)[0] == {c.memory_bytes}\n"
            f"    assert resource.getrlimit(resource.RLIMIT_NPROC)[0] == {c.max_processes}\n"
            f"    assert resource.getrlimit(resource.RLIMIT_FSIZE)[0] == {c.max_file_bytes}\n"
            f"    assert resource.getrlimit(resource.RLIMIT_CPU)[0] == {c.cpu_seconds}\n",
            encoding="utf-8",
        )
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "run_tests"), "run_tests",
            test_path=str(test_file),
        )
        assert result.success, result.error
        assert "2 passed" in result.data["stdout"], result.data["stdout"]

    def test_network_denied_inside_sandbox(self, tmp_path) -> None:
        probe = run_sandboxed_process(
            [sys.executable, "-c", "pass"],
            constraints=SandboxConstraints(deny_network=True),
            cwd=str(tmp_path),
        )
        if not probe["sandbox_report"]["applied"].get("netns"):
            pytest.skip(
                "user+network namespaces unavailable on this host"
            )
        test_file = tmp_path / "test_net.py"
        test_file.write_text(
            "import socket\n"
            "def test_no_network():\n"
            "    try:\n"
            "        socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
            "        raise AssertionError('network reachable — sandbox broken')\n"
            "    except OSError:\n"
            "        pass\n"
            "    names = sorted(n for _, n in socket.if_nameindex())\n"
            "    assert names == ['lo'], names\n",
            encoding="utf-8",
        )
        sim = self._sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "run_tests"), "run_tests",
            test_path=str(test_file),
        )
        assert result.success, result.error
        assert "1 passed" in result.data["stdout"], result.data["stdout"]


class TestManifestSandboxContract:
    """SANDBOXED_PROCESS requires the declarative constraint spec."""

    def test_sandboxed_process_requires_constraints(self) -> None:
        with pytest.raises(ToolManifestError, match="sandbox_constraints"):
            ToolManifest(
                name="x", version="1.0.0",
                execution_class=ExecutionClass.SANDBOXED_PROCESS,
            )

    def test_sandboxed_process_cannot_require_network(self) -> None:
        with pytest.raises(ToolManifestError, match="requires_network"):
            ToolManifest(
                name="x", version="1.0.0",
                execution_class=ExecutionClass.SANDBOXED_PROCESS,
                sandbox_constraints=SandboxConstraints(),
                requires_network=True,
            )

    def test_constraints_only_for_sandboxed(self) -> None:
        with pytest.raises(ToolManifestError, match="only valid"):
            ToolManifest(
                name="x", version="1.0.0",
                execution_class=ExecutionClass.READ_ONLY,
                sandbox_constraints=SandboxConstraints(),
            )

    def test_negative_constraints_rejected(self) -> None:
        with pytest.raises(ValueError):
            SandboxConstraints(memory_bytes=-1)
        with pytest.raises(ValueError):
            SandboxConstraints(readonly_binds=(("src",),))

    def test_run_tests_manifest_upgraded(self) -> None:
        manifest = builtin_manifests()["run_tests"]
        assert manifest.execution_class is ExecutionClass.SANDBOXED_PROCESS
        assert manifest.requires_network is False
        assert manifest.possible_side_effects == ()
        c = manifest.sandbox_constraints
        assert c is not None
        assert c.deny_network is True
        assert c.minimal_path is True
        assert c.pin_git_env is True
        assert c.cpu_seconds is not None
        assert c.memory_bytes is not None
        assert c.max_processes is not None
        assert c.max_file_bytes is not None
        assert c.strip_env_keywords == DEFAULT_SECRET_KEYWORDS
