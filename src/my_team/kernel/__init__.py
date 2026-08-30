"""Kernel — AgentOS（工作目录驱动）、事件协议、Process 两态、校验、内核态设备。"""

# 统一进程启动方式为 spawn（唯一做法，所有入口生效）：
# - 跨平台一致（macOS/Windows 本就 spawn；Linux 不再独走 fork）；
# - 提前暴露不可 pickle 的进程状态（fork 的共享内存会掩盖此类 bug）；
# - 对齐 Python 3.14 全平台默认 spawn/forkserver。
# 必须在任何 Queue/Process 创建前执行（本包 import 阶段即满足）。
# 代价：每次进程拉起重开解释器（慢一个量级）；入口须为可 import 的文件，
# 且顶层代码不得有副作用（子进程每次启动会重跑顶层）。
import multiprocessing

multiprocessing.set_start_method("spawn", force=True)

from my_team.kernel.agent_os import PROCESS_TYPES, AgentOS
from my_team.kernel.authority import Authority
from my_team.kernel.event_protocol import (
    VOID,
    ApplicationEvent,
    BaseEvent,
    Event,
    SystemEvent,
    SystemPayload,
)
from my_team.kernel.event_validator import EventError, validate_event
from my_team.kernel.journal import Journal
from my_team.kernel.process import (
    BucketDispatcher,
    KernelModeDevice,
    ProcessBase,
    UserModeProcess,
)
from my_team.kernel.process_handle import ProcessHandle

__all__ = [
    "AgentOS",
    "PROCESS_TYPES",
    "Authority",
    "Journal",
    "BucketDispatcher",
    "KernelModeDevice",
    "ProcessBase",
    "UserModeProcess",
    "ProcessHandle",
    "VOID",
    "BaseEvent",
    "SystemEvent",
    "SystemPayload",
    "ApplicationEvent",
    "Event",
    "EventError",
    "validate_event",
]
