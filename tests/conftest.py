import asyncio
import os

import pytest

from my_team.kernel.agent_os import AgentOS
from my_team.kernel.process_handle import ProcessHandle


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (need full sandbox access)",
    )


@pytest.fixture
def agent_os(tmp_path):
    """创建隔离的 AgentOS，teardown 时先关闭进程再清理目录。

    tmp_path 是 pytest 内置 fixture，自动清理。
    进程在 teardown 时被显式终止，确保目录删除前无活跃挂载点。
    """
    runtime_root = str(tmp_path / "runtime")
    os.makedirs(runtime_root)
    config_path = os.path.join(runtime_root, "config.yaml")
    with open(config_path, "w") as f:
        f.write(f"runtime_root: {runtime_root}\nagents: []\n")
    os_instance = AgentOS(config_path)
    yield os_instance
    # teardown: 终止所有用户态进程
    loop = asyncio.new_event_loop()
    for entity in list(os_instance.entities.values()):
        if isinstance(entity, ProcessHandle):
            loop.run_until_complete(entity.terminate(5))
    loop.close()
