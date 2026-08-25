"""设备协议与 Authority 包（SPEC §5.1，v0.11 N1a/N3）。

- ``base``：Device 协议（数据 + 工具 + 受控 uuid 注册；设备协议三条）
- ``authority``：Authority（注册中心 + 布线中心 + 两层 Grant + 单例/Owner）
- ``config``：配置设备（授予/策略/限额数据面）
- ``org``：组织架构设备（Authority 子类：岗位/边/授权读改写 + memory
  注入 + mount + 可替换，SPEC §5.8）

注意：``my_team.devices.authority.Authority`` 与旧
``my_team.authority``（8 域裁决模型，E2→N5）同名异义——前者是注册与
布线设备，后者是多方主张裁决引擎。
"""

from my_team.devices.authority import (
    Authority,
    AuthorityError,
    CapabilityGrant,
    Decision,
    DuplicateAuthorityError,
    GrantEffect,
    MembershipGrant,
    MemoryInjection,
    NotOwnerError,
    UnknownEntityError,
    authority_for,
    new_team_id,
)
from my_team.devices.base import (
    Device,
    EntityKind,
    InjectionDecl,
    RegisteredEntity,
    new_entity_id,
)
from my_team.devices.config import (
    ApprovalConfig,
    CapacityLimits,
    ConfigDevice,
    MemoryBudget,
)
from my_team.devices.org import (
    OrgChange,
    OrgDuplicatePosition,
    OrgError,
    OrgPermissionDenied,
    OrgPositionNotFound,
    OrgStructure,
)

__all__ = [
    "ApprovalConfig",
    "Authority",
    "AuthorityError",
    "CapabilityGrant",
    "CapacityLimits",
    "ConfigDevice",
    "Decision",
    "Device",
    "DuplicateAuthorityError",
    "EntityKind",
    "GrantEffect",
    "InjectionDecl",
    "MembershipGrant",
    "MemoryBudget",
    "MemoryInjection",
    "NotOwnerError",
    "OrgChange",
    "OrgDuplicatePosition",
    "OrgError",
    "OrgPermissionDenied",
    "OrgPositionNotFound",
    "OrgStructure",
    "RegisteredEntity",
    "UnknownEntityError",
    "authority_for",
    "new_entity_id",
    "new_team_id",
]
