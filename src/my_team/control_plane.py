"""HTTP Control Plane — minimal API for runtime management.

Provides a REST-like HTTP interface to control the simulation:
- GET  /status         — runtime status
- POST /start          — start the runtime
- POST /pause          — pause tick execution
- POST /resume         — resume tick execution
- POST /step?n=1       — execute n ticks synchronously
- POST /email          — send a human message to an agent
- GET  /agents         — agent tree and status
- GET  /tasks          — task tree

Uses stdlib http.server (no external dependencies).

Usage:
    from my_team.runtime import SimulationRuntime
    from my_team.control_plane import ControlPlane

    runtime = SimulationRuntime(sim, tick_duration_seconds=0.5)
    plane = ControlPlane(runtime, port=8080)
    plane.start()
    # API available at http://localhost:8080
    plane.stop()
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from my_team.runtime import SimulationRuntime

logger = logging.getLogger(__name__)


class _RuntimeHTTPServer(HTTPServer):
    """HTTPServer subclass that carries a SimulationRuntime reference."""

    def __init__(self, server_address: Any, handler: Any, runtime: SimulationRuntime) -> None:
        super().__init__(server_address, handler)
        self.runtime = runtime


class _RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the control plane."""

    server: _RuntimeHTTPServer  # type: ignore[assignment]

    # Suppress default access log
    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("HTTP %s", format % args)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/status":
            self._json_response(self.server.runtime.status)
        elif path == "/agents":
            self._handle_agents()
        elif path == "/tasks":
            self._handle_tasks()
        else:
            self._json_response({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        params = self._parse_params()

        if path == "/start":
            self.server.runtime.start()
            self._json_response({"ok": True})
        elif path == "/pause":
            self.server.runtime.pause()
            self._json_response({"ok": True})
        elif path == "/resume":
            self.server.runtime.resume()
            self._json_response({"ok": True})
        elif path == "/step":
            n = int(params.get("n", "1"))
            results = self.server.runtime.step(n)
            self._json_response({
                "ok": True,
                "ticks_executed": len(results),
            })
        elif path == "/email":
            self._handle_email()
        else:
            self._json_response({"error": "not found"}, status=404)

    def _handle_agents(self) -> None:
        sim = self.server.runtime.simulation
        agents = []
        for config in sim._agent_tree:
            agents.append({
                "agent_id": config.agent_id,
                "display_name": config.display_name,
                "role": config.role,
                "parent_id": config.parent_id,
                "tools": list(config.tools),
            })
        self._json_response({"agents": agents})

    def _handle_tasks(self) -> None:
        sim = self.server.runtime.simulation
        tasks = []
        for tid, task in sim._task_tree._tasks.items():
            tasks.append({
                "task_id": tid,
                "title": task.title,
                "status": task.status.value,
                "assignee_agent_id": task.assignee_agent_id,
            })
        self._json_response({"tasks": tasks})

    def _handle_email(self) -> None:
        try:
            body = self._read_body()
            sim = self.server.runtime.simulation
            to = body.get("to", [])
            subject = body.get("subject", "")
            message = body.get("message", "")

            if not to or not subject:
                self._json_response(
                    {"error": "missing 'to' or 'subject'"}, status=400,
                )
                return

            sim.human_control.send_email(
                to=to,
                subject=subject,
                body=message,
            )
            self._json_response({"ok": True})
        except Exception as e:
            self._json_response({"error": str(e)}, status=500)

    def _parse_params(self) -> dict[str, str]:
        if "?" not in self.path:
            return {}
        query = self.path.split("?", 1)[1]
        params = {}
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = v
        return params

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        result: dict[str, Any] = json.loads(raw)
        return result

    def _json_response(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS headers
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ControlPlane:
    """HTTP control plane for the simulation runtime.

    Provides a minimal REST API for external control and monitoring.
    """

    def __init__(
        self,
        runtime: SimulationRuntime,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self._runtime = runtime
        self._host = host
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        """Start the HTTP server in a background thread."""
        self._server = _RuntimeHTTPServer(
            (self._host, self._port), _RequestHandler, self._runtime,
        )
        self._server.allow_reuse_address = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="control-plane",
            daemon=True,
        )
        self._thread.start()
        logger.info("Control Plane started at %s", self.url)

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Control Plane stopped")

    def __repr__(self) -> str:
        return f"ControlPlane(url={self.url})"
