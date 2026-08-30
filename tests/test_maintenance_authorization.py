"""维护会话授权（快层，进程内，默认 pytest 即跑）。

拒绝路径在 spawn 之前裁决（_maintenance_session 的参数/权限校验先于
注册拉起），全程零子进程、亚秒级。真实维护闭环（spawn+沙箱）见
test_maintenance_session.py。用拦截 _kernel_emit 捕获 ack，避免 ack
路由触发懒进程拉起。
"""

import asyncio
import os

import my_team.agent  # noqa: F401  (注册 PROCESS_TYPES["agent"])
from my_team.kernel.agent_os import AgentOS


def _maintenance(source: str, target: str) -> dict:
    return {"source": source, "target": "kernel", "kind": "system",
            "payload": {"command": "maintenance_session",
                        "target_device": target}}


async def _setup(tmp_path) -> AgentOS:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runtime_root: {tmp_path}\nagents: []\n")
    agent_os = AgentOS(str(config_path))

    def spawn(emit):
        raise AssertionError("lazy 注册不应 spawn")

    await agent_os.register("worker", spawn, agent=True, position="worker",
                            lazy=True)
    return agent_os


def _capture_acks(agent_os: AgentOS) -> list[dict]:
    acks: list[dict] = []

    async def capture(source, event):
        acks.append(event)

    agent_os._kernel_emit = capture  # 实例级遮蔽：拦截 ack，不路由 → 不拉起
    return acks


def _deny_reason(acks: list[dict]) -> str:
    assert acks, "应回告 ack"
    ack = acks[-1]
    assert ack["payload"]["ok"] is False
    return ack["payload"]["error"]


def test_maintenance_denies_unauthorized(tmp_path):
    """无 maintain scope 的 agent 被拒，且不触发任何进程拉起。"""
    asyncio.run(_denies_unauthorized(tmp_path))


async def _denies_unauthorized(tmp_path):
    agent_os = await _setup(tmp_path)
    os.makedirs(os.path.join(str(tmp_path), "home", "echo"), exist_ok=True)
    acks = _capture_acks(agent_os)
    await agent_os._on_kernel(_maintenance("worker", "echo"))
    assert "维护权" in _deny_reason(acks)
    assert agent_os.entities["worker"]._process is None  # 全程零 spawn


def test_maintenance_denies_target_still_running(tmp_path):
    """目标仍在运行（未卸载）被拒。"""
    asyncio.run(_denies_target_still_running(tmp_path))


async def _denies_target_still_running(tmp_path):
    agent_os = await _setup(tmp_path)
    await agent_os.register("echo", lambda emit: None, tools=[], scopes=[],
                            lazy=True)
    await agent_os._grant("worker", "echo", "maintain")  # 先授维护权
    acks = _capture_acks(agent_os)
    await agent_os._on_kernel(_maintenance("worker", "echo"))
    assert "必须先卸载" in _deny_reason(acks)


def test_maintenance_denies_missing_home(tmp_path):
    """目标设备私家不存在被拒。"""
    asyncio.run(_denies_missing_home(tmp_path))


async def _denies_missing_home(tmp_path):
    agent_os = await _setup(tmp_path)
    await agent_os._grant("worker", "echo", "maintain")
    acks = _capture_acks(agent_os)
    await agent_os._on_kernel(_maintenance("worker", "echo"))
    assert "私家不存在" in _deny_reason(acks)
