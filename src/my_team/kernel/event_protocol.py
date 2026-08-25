"""Kernel：事件协议（TypedDict 约定）。

事件 = (source, target, kind, payload)。
- source: 宿主侧（Emitter）注入进程产出事件，值为该进程 pid
  （uuid 随机性保证不可冒充）
- target: 发送方填，决定发给谁（内核按它路由）
- kind: 层级——"system"（内核语义）| "application"（业务语义）
- payload: 任意；system 层约定 command 字段（terminate）
"""

from typing import Any, TypedDict, Union


class SystemPayload(TypedDict):
    command: str  # "terminate"


class BaseEvent(TypedDict):
    source: str
    target: str
    kind: str
    payload: Any


class SystemEvent(BaseEvent):
    kind: str  # "system"
    payload: SystemPayload


class ApplicationEvent(BaseEvent):
    kind: str  # "application"
    payload: Any


Event = Union[SystemEvent, ApplicationEvent]
