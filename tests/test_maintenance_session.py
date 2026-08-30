"""维护会话集成测试：真实子进程 + 沙箱（``pytest -m integration``）。

覆盖 卸载→编辑→重载 闭环与跨身份授权边界。设备源码按真实协议写
（Device 继承 UserModeProcess，接受 emit/runtime_root/identity）；
断言挂在可观察的进程状态上（进程存活、维护进程已挂载目标），
不再断言注册簿这种"空心"状态（旧假设备进沙箱即崩却仍 PASS）。

注意：进程必须显式 terminate。泄漏的非 daemon 子进程会在解释器退出时
被 multiprocessing._exit_function 无限 join——曾表现为"测试挂几分钟"。
"""

import asyncio
import os
import tempfile

import pytest

import my_team.agent  # noqa: F401  (注册 PROCESS_TYPES["agent"])
from my_team.kernel.agent_os import KERNEL_IDENTITY, AgentOS
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
SCOPES = [
    {"token": "maintain", "default": True, "explanation": "维护"},
    {"token": "public", "default": True, "explanation": "basic"},
]
'''


def _cmd(command, **payload):
    return {
        "source": KERNEL_IDENTITY,
        "target": "kernel",
        "kind": "system",
        "payload": {"command": command, **payload},
    }


def _write_device(device_home: str, source: str) -> None:
    os.makedirs(device_home, exist_ok=True)
    with open(os.path.join(device_home, "device.py"), "w") as f:
        f.write(source)


async def _install(agent_os: AgentOS, identity: str, grants: list[str]) -> None:
    await agent_os._on_kernel(_cmd("install_device", identity=identity,
                                   grants=grants, options={}))


async def _uninstall(agent_os: AgentOS, identity: str) -> None:
    await agent_os._on_kernel(_cmd("uninstall_device", identity=identity))


async def _maintenance(agent_os: AgentOS, actor: str, target: str) -> None:
    event = _cmd("maintenance_session", target_device=target)
    event["source"] = actor
    await agent_os._on_kernel(event)


async def _teardown(agent_os: AgentOS) -> None:
    """终止全部用户态进程（见文件头注释：泄漏进程卡解释器退出）。"""
    for entity in list(agent_os.entities.values()):
        if isinstance(entity, ProcessHandle):
            await entity.terminate(5)


@pytest.mark.integration
def test_maintenance_unload_edit_reload_roundtrip():
    asyncio.run(_maintenance_unload_edit_reload_roundtrip())


async def _maintenance_unload_edit_reload_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.yaml")
        with open(config_path, "w") as f:
            f.write(f"""
runtime_root: {tmpdir}
agents:
  - identity: worker
    type: agent
    options:
      position: worker
""")
        os.makedirs(os.path.join(tmpdir, "home", "worker"), exist_ok=True)
        _write_device(os.path.join(tmpdir, "home", "echo"), DEVICE_SOURCE)

        agent_os = AgentOS(config_path)
        try:
            await agent_os.setup()

            # 装载：设备进程真存活（非空心——旧假设备进沙箱即崩）
            await _install(agent_os, "echo", grants=["worker"])
            echo = agent_os.entities["echo"]
            assert isinstance(echo, ProcessHandle)
            assert echo._process.is_alive()
            assert echo._process._load_spec is not None  # 设备（带装载描述）

            # 卸载：实体摘除；maintain scope 跨卸载保留（维护授权存续）
            await _uninstall(agent_os, "echo")
            assert "echo" not in agent_os.entities

            # 维护：worker 持 maintain scope，进程真以双锚点拉起并存活
            await _maintenance(agent_os, "worker", "echo")
            worker = agent_os.entities["worker"]
            assert isinstance(worker, ProcessHandle)
            assert worker._process.maintenance_device == "echo"
            assert worker._process.is_alive()

            # 重装：设备再次存活
            await _install(agent_os, "echo", grants=["worker"])
            echo = agent_os.entities["echo"]
            assert isinstance(echo, ProcessHandle)
            assert echo._process.is_alive()
        finally:
            await _teardown(agent_os)


@pytest.mark.integration
def test_maintenance_authorization_boundary():
    asyncio.run(_maintenance_authorization_boundary())


async def _maintenance_authorization_boundary():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.yaml")
        with open(config_path, "w") as f:
            f.write(f"""
runtime_root: {tmpdir}
agents:
  - identity: worker1
    type: agent
    options:
      position: worker1
  - identity: worker2
    type: agent
    options:
      position: worker2
""")
        for aid in ("worker1", "worker2"):
            os.makedirs(os.path.join(tmpdir, "home", aid), exist_ok=True)
        _write_device(os.path.join(tmpdir, "home", "echo"), DEVICE_SOURCE)

        agent_os = AgentOS(config_path)
        try:
            await agent_os.setup()

            # worker1 布线 echo（含 maintain）；worker2 无任何授权
            await _install(agent_os, "echo", grants=["worker1"])
            echo = agent_os.entities["echo"]
            assert isinstance(echo, ProcessHandle) and echo._process.is_alive()

            # 无授权者：维护被拒，实体状态不被触碰（echo 仍在运行、worker2 未被顶替）
            before = agent_os.entities["worker2"]
            await _maintenance(agent_os, "worker2", "echo")
            assert "echo" in agent_os.entities
            assert agent_os.entities["worker2"] is before
            assert agent_os.entities["worker2"]._process.maintenance_device is None

            # 授权者：卸载后维护，维护进程真挂载目标并存活
            await _uninstall(agent_os, "echo")
            assert "echo" not in agent_os.entities
            await _maintenance(agent_os, "worker1", "echo")
            worker1 = agent_os.entities["worker1"]
            assert isinstance(worker1, ProcessHandle)
            assert worker1._process.maintenance_device == "echo"
            assert worker1._process.is_alive()
        finally:
            await _teardown(agent_os)
