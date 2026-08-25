"""Device protocol — 设备一般结构（SPEC §5.1，N1a）。

设备 = 数据 + 工具（带 uuid 注册机制）。本模块定义：

- ``RegisteredEntity``：设备声明的一个受控实体（数据条目/范围、工具/
  工具包），带 uuid；注册时可选声明"授予生效后注入记忆的 content"
  （§5.1 三条之三：注册即声明注入内容——注入记忆非数据全量，解释权
  在设备内部）；
- ``Device``：设备基类。设备**不维护账本**（三条之一：只持当前状态，
  Journal 是唯一事实源，§3.2/§5.9）；设备依赖用接口声明（N1c 落实）；
  身份落字段是设备职责（三条之二：设备工具把内核构造的调用上下文
  身份落为自己的数据字段，接线在 N1c 工具实现中）。

设备的受控 uuid 经 ``Device.register_to(authority)`` 提交到 Authority
注册中心（§5.1 注册中心）；授权判定（能不能用）由 Authority 布线中心
的两层 Grant 求值（§5.1），设备不自行判权。

Design references:
- SPEC §1.8（ACL 主体 = position）/ §3.5（效果级策略与 ACL）/
  §5.1（设备协议与 Authority）
- KANBAN/TODO/2026-08-24-device-protocol-authority.md（N1a）
- 2026-08-24 定案：设备协议三条（不维护账本 / 身份落字段是设备职责 /
  注册即声明注入内容）
"""

from __future__ import annotations

import uuid
from abc import ABC
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from my_team.devices.authority import Authority


class EntityKind(str, Enum):
    """受控实体种类（§5.1）：数据条目/范围、单工具、工具包。"""

    DATA = "data"
    TOOL = "tool"
    TOOLPACK = "toolpack"


@dataclass(frozen=True)
class InjectionDecl:
    """注册即声明注入内容（§5.1 三条之三）。

    设备声明"授予生效后注入 agent 记忆的 content"——引导 Agent 使用
    （如页面权限说明），**非数据全量**；注入内容的解释权在设备内部，
    授权判定仍查 Authority。
    """

    content: str
    # 注入后在记忆中的来源段标签（N4 使用，如 [SKILL_INSTRUCTION]）。
    source_tag: str = ""


@dataclass(frozen=True)
class RegisteredEntity:
    """设备声明的一个受控实体（注册中心的基本单元）。"""

    entity_id: str  # uuid4，设备注册时生成
    device_id: str  # 声明它的设备
    kind: EntityKind
    label: str  # 设备内可读名（业务标签，非权限依据）
    injection: InjectionDecl | None = None


class Device(ABC):
    """设备一般结构 = 数据 + 工具（§5.1）。

    数据任意内部结构；需独立权限控制的条目/范围、工具/工具包经
    ``register_entity`` 注册为受控 uuid。设备不维护账本（重放源唯一 =
    Journal）；设备依赖经接口声明（本类为协议骨架，数据面由子类承载）。
    """

    def __init__(self, device_id: str | None = None) -> None:
        self.device_id = device_id or str(uuid.uuid4())
        self._entities: dict[str, RegisteredEntity] = {}

    def register_entity(
        self,
        kind: EntityKind,
        label: str,
        injection: InjectionDecl | None = None,
        entity_id: str | None = None,
    ) -> str:
        """声明一个受控实体，返回其 uuid（未注册前对 Authority 不可见）。

        ``injection`` 即"注册即声明注入内容"（三条之三）。

        ``entity_id`` 显式给出时即 adopt 机制（N1c：设备采用
        uuid5 派生值而非随机 uuid4，保证 manifest_hash 稳定）。
        """
        eid = entity_id if entity_id is not None else str(uuid.uuid4())
        self._entities[eid] = RegisteredEntity(
            entity_id=eid,
            device_id=self.device_id,
            kind=kind,
            label=label,
            injection=injection,
        )
        return eid

    @property
    def entities(self) -> Mapping[str, RegisteredEntity]:
        """本设备声明的受控实体（只读视图）。"""
        return MappingProxyType(self._entities)

    def register_to(self, authority: "Authority") -> None:
        """把本设备全部受控 uuid 提交到 Authority 注册中心（§5.1）。"""
        authority.accept_device(self)

    def injected_content_for(self, entity_id: str) -> InjectionDecl | None:
        """设备对某受控实体的注入声明（无声明返回 None）。"""
        entity = self._entities.get(entity_id)
        return entity.injection if entity is not None else None


def new_entity_id() -> str:
    """受控实体 uuid 分配（uuid4，注册即分配）。"""
    return str(uuid.uuid4())
