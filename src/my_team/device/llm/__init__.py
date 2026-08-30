"""LLM 设备：为其他进程提供 LLM API 能力的服务进程。"""

from my_team.device.llm.device import LLM_REQUEST, LLM_RESULT, LLMDevice

__all__ = ["LLMDevice", "LLM_REQUEST", "LLM_RESULT"]
