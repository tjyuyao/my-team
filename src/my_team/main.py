"""入口：配置驱动组装 AgentOS。

用法：my-team [config.yaml]
"""

import asyncio
import sys

import my_team.agent  # noqa: F401  注册 DEVICE_TYPES
import my_team.device.utils  # noqa: F401  注册 DEVICE_TYPES
from my_team.kernel import AgentOS


def main():
    config = sys.argv[1] if len(sys.argv) > 1 else "team.yaml"
    aos = AgentOS(config)
    asyncio.run(aos.setup())
    aos.run()
