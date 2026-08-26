"""入口：组装 AgentOS（agent 拓扑来自配置；设备由工作目录运行期装载）。

用法：my-team [config.yaml]
"""

import asyncio
import sys

import my_team.agent  # noqa: F401  注册 PROCESS_TYPES（进程类型；设备走工作目录）
from my_team.kernel import AgentOS


def main():
    config = sys.argv[1] if len(sys.argv) > 1 else "team.yaml"
    aos = AgentOS(config)
    asyncio.run(aos.setup())
    aos.run()
