"""sandboxed_python execution levels L0/L1 + physical cancel.

v0.8.0 P1-7 + P2-10 (SPEC §8.7「执行等级」):

- L0 python_compute: subprocess, restricted builtins + import gate,
  `-I` isolated mode, JSON inputs → structured result
- L1 python_transform: temp sandbox workspace (read-only input copies
  from the FROZEN view, writable output dir), artifact manifest
- Honest classification: LOCAL_PROCESS = accident prevention, NOT a
  security boundary (asserted via manifest fields)
- Physical cancel: a running LOCAL_PROCESS subprocess is killed by
  process group (executor_cancel_requested/confirmed=True)
"""

from __future__ import annotations

import threading
import time
import uuid

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_tree import AgentTree
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SendEmailIntent, SubmitToolRequest
from my_team.pending_ops import OpStatus
from my_team.python_worker import run_python_compute, run_python_transform
from my_team.simulation import Simulation
from my_team.tool_manifest import ExecutionClass


def _make_tree(tools: list[str]) -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"] + tools,
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


def _bootstrap(sim: Simulation, agent_id: str) -> None:
    from my_team.models.activation import WakeCondition, WakeEventType, WakeupEvent
    cond = sim.scheduler.get_wake_condition(agent_id)
    sim.scheduler.update_wake_condition(
        agent_id,
        WakeCondition(
            event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
            wake_at_tick=0, visible_at_tick=0,
        ),
    )
    sim.scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.BOOTSTRAP,
        target_agent_id=agent_id,
        tick=0, visible_at_tick=0,
        source_agent_id="system",
    ))


class ToolAgent(BaseAgent):
    """Submits a python tool request; reports the outcome via email."""

    tool_name = "python_compute"
    arguments: dict = {}

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_tool_result
        ):
            r = continuation.last_tool_result
            return [SendEmailIntent(
                agent_id=self._agent_id,
                to=["agent.research"],
                subject="[PY DONE]",
                body=str(r.get("error", r.get("result", "ok"))),
            )]
        return [SubmitToolRequest(
            agent_id=self._agent_id,
            tool_name=self.tool_name,
            arguments=self.arguments,
            timeout_ticks=50,
        )]


class TestPythonComputeWorker:
    """L0 worker: restricted environment, structured result."""

    def test_basic_computation(self) -> None:
        res = run_python_compute(
            code="result = sum(inputs['values'])",
            inputs={"values": [1, 2, 3]},
        )
        assert res["success"] is True
        assert res["result"] == 6

    def test_stdlib_allowlist_usable(self) -> None:
        res = run_python_compute(
            code=(
                "import statistics\n"
                "result = {'mean': statistics.mean(inputs['v'])}"
            ),
            inputs={"v": [1, 2, 3]},
        )
        assert res["success"] is True
        assert res["result"] == {"mean": 2.0}

    def test_import_gate_denies_os(self) -> None:
        res = run_python_compute(
            code="import os\nresult = os.getpid()",
            inputs={},
        )
        assert res["success"] is False
        assert "not in the allowed set" in res.get("error", "")

    def test_restricted_builtins_deny_open(self) -> None:
        res = run_python_compute(
            code="result = open('/etc/hostname').read()",
            inputs={},
        )
        assert res["success"] is False
        assert "open" in res.get("error", "")

    def test_timeout_kills_process(self) -> None:
        started = time.monotonic()
        res = run_python_compute(
            code="while True: pass",
            inputs={},
            timeout_ms=200,
        )
        assert res["timed_out"] is True
        assert res["success"] is False
        assert time.monotonic() - started < 10

    def test_non_json_result_rejected(self) -> None:
        res = run_python_compute(
            code="result = {1, 2, 3}",
            inputs={},
        )
        assert res["success"] is False
        assert "JSON-serializable" in res.get("error", "")

    def test_user_code_exception_surfaced(self) -> None:
        res = run_python_compute(
            code="result = 1 / 0",
            inputs={},
        )
        assert res["success"] is False
        assert "ZeroDivisionError" in res.get("error", "")


class TestPythonTransformWorker:
    """L1 worker: temp sandbox workspace + artifact manifest."""

    def test_reads_input_copy_and_writes_artifact(self) -> None:
        res = run_python_transform(
            code=(
                "import csv, json\n"
                "rows = list(csv.DictReader(open(input_dir + '/data.csv')))\n"
                "result = {'rows': len(rows)}\n"
                "with open(output_dir + '/report.json', 'w') as f:\n"
                "    json.dump(rows, f)\n"
            ),
            inputs={},
            input_files={
                "data.csv": "name,score\nalice,90\nbob,80\n",
            },
        )
        assert res["success"] is True
        assert res["result"] == {"rows": 2}
        artifacts = res["artifacts"]
        assert len(artifacts) == 1
        art = artifacts[0]
        assert art["path"] == "report.json"
        assert art["size"] == len(art["content"])
        assert '"alice"' in art["content"]
        # hash matches content
        import hashlib
        assert art["hash"] == hashlib.sha256(
            art["content"].encode("utf-8"),
        ).hexdigest()

    def test_result_without_artifacts(self) -> None:
        res = run_python_transform(
            code="result = {'computed': inputs['x'] * 2}",
            inputs={"x": 21},
            input_files={},
        )
        assert res["success"] is True
        assert res["result"] == {"computed": 42}
        assert res["artifacts"] == []


class TestPythonToolsInSimulation:
    """python tools through the intent + dispatch pipeline."""

    def test_python_compute_e2e(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["python_compute"]))
        agent = ToolAgent("agent.research")
        agent.tool_name = "python_compute"
        agent.arguments = {
            "code": "result = {'total': sum(inputs['values'])}",
            "inputs": {"values": [10, 20, 30]},
        }
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()  # submit → dispatch executes in-process
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.status == OpStatus.COMPLETED
        assert op.result["result"] == {"total": 60}
        assert op.result["success"] is True

        sim.run_tick()  # ingest delivers → agent emails the result
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert "{'total': 60}" in email.body

    def test_python_transform_reads_frozen_view(self) -> None:
        """input_files resolve from the FROZEN snapshot, not the live
        filesystem."""
        sim = Simulation(agent_tree=_make_tree(["python_transform"]))
        path = f"d-{uuid.uuid4().hex[:8]}.csv"
        target = sim._private_store.agent_home("agent.research") / path
        target.write_text("a,b\n1,2\n", encoding="utf-8")

        agent = ToolAgent("agent.research")
        agent.tool_name = "python_transform"
        agent.arguments = {
            "code": (
                "import csv\n"
                "rows = list(csv.DictReader(open(input_dir + '/" + path + "')))\n"
                "result = {'rows': len(rows)}\n"
            ),
            "input_files": {path: ""},
        }
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.status == OpStatus.COMPLETED
        assert op.result["success"] is True
        assert op.result["result"] == {"rows": 1}

    def test_transform_missing_input_rejected(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["python_transform"]))
        agent = ToolAgent("agent.research")
        agent.tool_name = "python_transform"
        agent.arguments = {
            "code": "result = {}",
            "input_files": {"nope.csv": ""},
        }
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        sim.run_tick()
        op = sim._pending_ops.get_by_agent("agent.research")[0]
        assert op.status == OpStatus.COMPLETED
        assert op.result["success"] is False
        assert op.result["error_code"] == "invalid_argument"
        assert "not in frozen workspace view" in op.result["error"]

    def test_manifests_honest_classification(self) -> None:
        """LOCAL_PROCESS + no SANDBOXED_PROCESS claim + cancel support."""
        sim = Simulation(agent_tree=_make_tree([]))
        for name in ("python_compute", "python_transform"):
            manifest = sim._tool_registry.get_manifest(name)
            assert manifest is not None
            assert manifest.execution_class is ExecutionClass.LOCAL_PROCESS
            assert manifest.requires_network is False
            assert manifest.supports_cancel is True
        assert sim._tool_registry.get_manifest("python_compute") \
            .filesystem_scopes == ("none",)
        assert sim._tool_registry.get_manifest("python_transform") \
            .filesystem_scopes == ("workspace",)
        # No builtin tool claims SANDBOXED_PROCESS (honesty gate)
        for m in sim._tool_registry.manifests():
            assert m.execution_class is not ExecutionClass.SANDBOXED_PROCESS


class TestPhysicalCancel:
    """P2-10: running LOCAL_PROCESS subprocesses are killed on cancel."""

    def test_cancel_kills_python_worker_process_group(self) -> None:
        sim = Simulation(agent_tree=_make_tree(["python_compute"]))
        agent = ToolAgent("agent.research")
        agent.tool_name = "python_compute"
        agent.arguments = {
            "code": "import time\nwhile True: time.sleep(1)\nresult = 1",
            "inputs": {},
        }
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent
        _bootstrap(sim, "agent.research")

        # The worker blocks the tick inside dispatch — run it in a
        # thread and cancel from outside (as the harness/human would).
        thread = threading.Thread(target=sim.run_tick)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not sim._active_processes:
                if time.monotonic() > deadline:
                    raise AssertionError("worker process never registered")
                time.sleep(0.01)
            request_id = next(iter(sim._active_processes))

            result = sim.cancel_operation(request_id)
            assert result.accepted
            assert result.executor_cancel_requested is True
            assert result.executor_cancel_confirmed is True
            assert result.result_fenced is True
        finally:
            thread.join(timeout=15)
        assert not thread.is_alive()

        # Op removed; agent woken with the cancellation notice, not a
        # result
        assert sim._pending_ops.get_by_agent("agent.research") == []
        rs = sim._agent_runtime_states["agent.research"]
        assert rs.continuation.last_tool_result is not None
        assert rs.continuation.last_tool_result.get("cancelled") is True

    def test_run_tests_declares_supports_cancel(self) -> None:
        sim = Simulation(agent_tree=_make_tree([]))
        manifest = sim._tool_registry.get_manifest("run_tests")
        assert manifest is not None
        assert manifest.supports_cancel is True
