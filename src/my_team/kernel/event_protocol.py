"""Kernel：事件协议（TypedDict 约定）。

事件 = (source, target, kind, payload)。
- source: 宿主读侧盖章（reader 按出站队列归属注入）进程产出事件，值为
  该进程身份（进程内不存在可改写身份字段）
- target: 发送方填，决定发给谁（内核按它路由）；必须指向已注册进程
  （否则校验失败，走 print 路径）
- kind: 层级——"system"（内核语义）| "application"（业务语义）
- payload: 任意；system 层约定 command 字段（terminate / install_device /
  uninstall_device / grant_scope / revoke_scope）
"""

from typing import Any, NotRequired, TypedDict, Union


class SystemPayload(TypedDict):
    command: str  # "terminate"


class BaseEvent(TypedDict):
    source: str
    target: str
    kind: str
    payload: Any
    receipt: NotRequired[bool]


class SystemEvent(BaseEvent):
    kind: str  # "system"
    payload: SystemPayload


class ApplicationEvent(BaseEvent):
    kind: str  # "application"
    payload: Any


Event = Union[SystemEvent, ApplicationEvent]

# respond 契约的合法"无话可说"返回值：worker 本地消化，不上总线。
# 用字符串常量（"VOID"）而非 object()：可 pickle、可比较、可入 JSON，
# 适应更灵活的场景（如跨进程传递、序列化记录）。
# 与 target 取值互斥：协议不存在 "void" 空目标（target 未注册的事件由内核丢弃）。
VOID = "VOID"
