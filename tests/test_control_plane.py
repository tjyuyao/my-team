"""Tests for T5: HTTP Control Plane.

Date: 2026-08-18
"""

from __future__ import annotations

import json
import time
import urllib.request

from my_team.agent_tree import AgentTree
from my_team.control_plane import ControlPlane
from my_team.runtime import SimulationRuntime
from my_team.simulation import Simulation


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls"],
                "can_delegate": False,
                "metadata": {"bootstrap": True},
            },
        ],
    })


class TestControlPlane:
    _port_counter = 18100

    def _setup(self):
        TestControlPlane._port_counter += 1
        port = TestControlPlane._port_counter
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim)
        plane = ControlPlane(runtime, port=port)
        plane.start()
        time.sleep(0.1)  # let server start
        return sim, runtime, plane, port

    def _get(self, path: str, port: int) -> dict:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read())

    def _post(self, path: str, port: int, body: dict | None = None) -> dict:
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())

    def test_status_endpoint(self):
        _sim, _runtime, _plane, port = self._setup()
        result = self._get("/status", port)
        assert "tick" in result
        assert "state" in result
        assert result["tick"] == 0

    def test_agents_endpoint(self):
        _sim, _runtime, _plane, port = self._setup()
        result = self._get("/agents", port)
        assert "agents" in result
        assert len(result["agents"]) == 1
        assert result["agents"][0]["agent_id"] == "agent.root"

    def test_step_endpoint(self):
        _sim, _runtime, _plane, port = self._setup()
        result = self._post("/step?n=2", port)
        assert result["ok"] is True
        assert result["ticks_executed"] == 2

    def test_pause_resume_endpoints(self):
        _sim, _runtime, _plane, port = self._setup()
        self._post("/pause", port)
        status = self._get("/status", port)
        assert status["state"] == "paused"

        self._post("/resume", port)
        status = self._get("/status", port)
        assert status["state"] == "running"

    def test_email_endpoint(self):
        _sim, _runtime, _plane, port = self._setup()
        result = self._post("/email", port, {
            "to": ["agent.root"],
            "subject": "Test",
            "message": "Hello",
        })
        assert result["ok"] is True

    def test_404_for_unknown_path(self):
        _sim, _runtime, _plane, port = self._setup()
        try:
            self._get("/nonexistent", port)
        except urllib.error.HTTPError as e:
            assert e.code == 404

    def test_start_stop_lifecycle(self):
        TestControlPlane._port_counter += 1
        port = TestControlPlane._port_counter
        sim = Simulation(agent_tree=_make_tree())
        runtime = SimulationRuntime(sim, tick_duration_seconds=0.05)
        plane = ControlPlane(runtime, port=port)
        plane.start()
        time.sleep(0.1)
        self._post("/start", port)
        time.sleep(0.2)
        plane.stop()
