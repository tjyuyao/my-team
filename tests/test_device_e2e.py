"""设备端到端（集成，``pytest -m integration``）：真实 spawn + 沙箱下
事件 → respond → ChildWriter → 宿主 reader 读侧盖章 → 事件总线。

证明完整管道（含沙箱隔离）真正跑通，断言真实响应内容而非注册簿：
宿主投递 ping → 设备在沙箱内 respond → pong 回到总线且 source=echo
（宿主读侧盖章，进程内无可改写身份字段）。
"""

import asyncio
import os

import pytest

from my_team.kernel.agent_os import KERNEL_IDENTITY
from my_team.kernel.process_handle import ProcessHandle

DEVICE_SOURCE = '''\
from my_team.kernel.process import UserModeProcess


class Device(UserModeProcess):
    def __init__(self, emit, *, runtime_root, identity):
        super().__init__(emit, 0, runtime_root, identity=identity)

    async def respond(self, event):
        payload = event["payload"]
        if payload.get("command") == "ping":
            return {"target": event["source"], "kind": "application",
                    "payload": {"command": "pong", "data": payload.get("data")}}
        return {"target": event["source"], "kind": "application",
                "payload": {"command": "error",
                            "error": f"未知命令: {payload.get('command')!r}"}}


TOOLS = [{"name": "echo", "trigger": ["echo"]}]
SCOPES = [{"token": "public", "default": True, "explanation": "basic"}]
'''


@pytest.mark.integration
def test_device_event_round_trip(agent_os):
    asyncio.run(_device_event_round_trip(agent_os))


async def _device_event_round_trip(agent_os):
    device_home = os.path.join(agent_os.runtime_root, "home", "echo")
    os.makedirs(device_home, exist_ok=True)
    with open(os.path.join(device_home, "device.py"), "w") as f:
        f.write(DEVICE_SOURCE)

    await agent_os._on_kernel({
        "source": KERNEL_IDENTITY, "target": "kernel", "kind": "system",
        "payload": {"command": "install_device", "identity": "echo",
                    "grants": ["worker"], "options": {}},
    })
    handle = agent_os.entities["echo"]
    assert isinstance(handle, ProcessHandle)
    assert handle._process.is_alive()

    # 投递 ping → 设备 respond → 宿主 reader 盖章 → 事件总线
    handle.deliver({
        "source": "caller", "target": "echo", "kind": "application",
        "payload": {"command": "ping", "data": "你好"},
    })
    response = agent_os.event_bus.get(timeout=15)
    assert response["source"] == "echo"          # 宿主读侧盖章
    assert response["target"] == "caller"
    assert response["payload"]["command"] == "pong"
    assert response["payload"]["data"] == "你好"
