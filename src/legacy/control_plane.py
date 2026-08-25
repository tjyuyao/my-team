"""HTTP Control Plane — 内核通用操作台 + 设备 UI 插件注册（SPEC §3.7/§10）。

Control Plane 是 Owner/人类的**通用操作台**（启停/消息/审批/审计/看板），
**属内核**：纯逻辑 + 框架，不持有业务数据（业务数据在各设备）。设备可
经设备接口（``DeviceUIPlugin``）把自己的 UI 模块（前端模块名 + 后端
handler）插件化注册到 Control Plane（§3.7/§5.1 设备协议）；注册表在
本模块（``UIRegistry``），``GET /ui/modules`` 输出渲染清单。

REST-like HTTP 接口（§10 草案子集）：
- GET  /status | /agents | /tasks | /ui/modules
- POST /start | /pause | /resume | /step | /email

存量业务端点（/agents、/tasks、/email）仍直连 simulation 层内部——旧版
操作台的迁移过渡；本卡内核化增量（UI 插件注册/渲染、/ui/modules、
设备声明模块）不持有业务数据。Uses stdlib http.server.

Usage:
    from my_team.runtime import SimulationRuntime
    from my_team.control_plane import ControlPlane

    runtime = SimulationRuntime(sim, tick_duration_seconds=0.5)
    plane = ControlPlane(runtime, port=8080)
    plane.register_device_ui(org_device)   # 设备 UI 插件注册（§3.7）
    plane.start()
    # API available at http://localhost:8080
    plane.stop()
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol

from my_team.runtime import SimulationRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UIModule:
    """设备 UI 插件声明（SPEC §3.7：前端模块名 + 后端 handler）。

    - ``module_name``：注册表键（如 ``"org.positions"``）；
    - ``frontend_module``：前端模块名（如 ``"org/positions-panel"``）；
    - ``backend_handler``：渲染时调用的后端逻辑（无则纯前端模块）；
    - ``description``：模块说明。
    """

    module_name: str
    frontend_module: str
    backend_handler: Callable[[], dict[str, Any]] | None = None
    description: str = ""


class DeviceUIPlugin(Protocol):
    """设备 UI 插件接口（§3.7「经设备接口声明」）。

    设备实现该结构即声明自己的 UI 模块（如组织架构设备的岗位管理页、
    KB 设备的知识编辑页）；Control Plane 经 ``register_device_ui``
    插件化挂载。
    """

    device_id: str
    ui_modules: list[UIModule]


class UIRegistry:
    """设备 UI 插件注册表（注册表在 Control Plane，§3.7）。

    设备经插件接口注册（``register_device``）；Control Plane 渲染时
    输出前端模块名 + 后端 handler 结果。本类不持有业务数据——模块
    内容由设备在 handler 中自绘。
    """

    def __init__(self) -> None:
        self._modules: dict[str, tuple[str, UIModule]] = {}

    def register(self, device_id: str, module: UIModule) -> None:
        if module.module_name in self._modules:
            existing = self._modules[module.module_name][0]
            raise ValueError(
                f"UI 模块 {module.module_name!r} 已注册（设备 {existing}）"
            )
        self._modules[module.module_name] = (device_id, module)

    def register_device(self, device: DeviceUIPlugin) -> None:
        """注册设备声明的全部 UI 模块（经设备接口声明，§3.7）。"""
        for module in device.ui_modules:
            self.register(device.device_id, module)

    def modules(self) -> dict[str, tuple[str, UIModule]]:
        """已注册模块（只读副本）：module_name -> (device_id, module)。"""
        return dict(self._modules)

    def manifest(self) -> list[dict[str, Any]]:
        """前端渲染清单（GET /ui/modules 的数据）。"""
        return [
            {
                "module_name": name,
                "frontend_module": module.frontend_module,
                "device_id": device_id,
                "description": module.description,
            }
            for name, (device_id, module) in self._modules.items()
        ]


class _RuntimeHTTPServer(HTTPServer):
    """HTTPServer subclass that carries a SimulationRuntime + UI registry."""

    def __init__(
        self,
        server_address: Any,
        handler: Any,
        runtime: SimulationRuntime,
        ui_registry: UIRegistry,
    ) -> None:
        super().__init__(server_address, handler)
        self.runtime = runtime
        self.ui_registry = ui_registry


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
        elif path == "/ui/modules":
            # 设备 UI 插件清单（§3.7：注册表在 Control Plane，渲染输出）。
            self._json_response({"modules": self.server.ui_registry.manifest()})
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
    """内核通用操作台（启停/消息/审批/审计/看板）+ 设备 UI 插件注册。

    纯逻辑 + 框架，不持有业务数据；设备经 ``register_device_ui`` 注册
    UI 模块（§3.7），``render_ui_modules`` 渲染插件模块。
    """

    def __init__(
        self,
        runtime: SimulationRuntime | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self._runtime = runtime
        self._host = host
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.ui_registry = UIRegistry()

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def register_device_ui(self, device: DeviceUIPlugin) -> None:
        """设备经插件接口注册 UI 模块（§3.7/§5.1 设备协议）。"""
        self.ui_registry.register_device(device)

    def ui_manifest(self) -> list[dict[str, Any]]:
        """UI 插件清单（前端渲染数据）：模块名 → 前端模块 + 归属设备。"""
        return self.ui_registry.manifest()

    def render_ui_modules(self) -> dict[str, dict[str, Any]]:
        """渲染全部已注册 UI 模块（§3.7：Control Plane 渲染对应模块）。

        前端模块名 + 后端 handler 输出；无 handler 的模块只给前端声明。
        """
        rendered: dict[str, dict[str, Any]] = {}
        for module_name, (device_id, module) in self.ui_registry.modules().items():
            entry: dict[str, Any] = {
                "frontend_module": module.frontend_module,
                "device_id": device_id,
                "description": module.description,
            }
            if module.backend_handler is not None:
                entry["data"] = module.backend_handler()
            rendered[module_name] = entry
        return rendered

    def start(self) -> None:
        """Start the HTTP server in a background thread."""
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("Control Plane 需要 SimulationRuntime 才能启动")
        self._server = _RuntimeHTTPServer(
            (self._host, self._port), _RequestHandler, runtime, self.ui_registry,
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
