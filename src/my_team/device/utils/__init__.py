"""Utils 设备：通用工具执行（weather/time 演示）。"""

from my_team.device.utils.device import UtilsDevice
from my_team.kernel.agent_os import DEVICE_TYPES

DEVICE_TYPES["utils"] = UtilsDevice

__all__ = ["UtilsDevice"]
