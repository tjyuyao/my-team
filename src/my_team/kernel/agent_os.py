"""Kernel：AgentOS 与事件调度（配置驱动）。

统一通信协议：事件 = (source, target, kind, payload)。
- source: 宿主侧（Emitter）注入进程产出事件，值为该进程身份（不可冒充）
- target: 发送方填，决定发给谁（内核按它路由）；必须指向已注册进程
- kind: "system"（内核语义，payload.command）| "application"（业务语义）
- payload: 任意；system 层约定 command（terminate）

三态：
- kernel：调度（校验 → 记录 → 路由）+ 托管内核态设备（Authority/Journal）；
- agent/device：进程；用户态 = 真实子进程，内核态 = 与 kernel 同进程。

配置驱动：整个 team 的配置来自一个文件（工具定义也在其中，数据化）；
热加载：配置文件变化 → 重新登记能力声明 → 重新注入工具条目（diff 驱动）。
"""

import asyncio
import multiprocessing as mp
import os
import time

import yaml

from my_team.kernel.authority import Authority
from my_team.kernel.event_protocol import VOID, Event
from my_team.kernel.event_validator import (
    DEFAULT_RULES,
    EventError,
    SourceRegistered,
    TargetRegistered,
    validate_event,
)
from my_team.kernel.journal import Journal
from my_team.kernel.process import KernelModeDevice, BucketDispatcher
from my_team.kernel.process_handle import ProcessHandle

# 设备类型注册表：config "type" → 设备类（构造参数经 options）。
# 设备模块在 import 时自注册（如 my_team.device.utils、my_team.agent）。
DEVICE_TYPES: dict[str, type] = {}


class AgentOS:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load(config_path)
        self.entities: dict[str, ProcessHandle | KernelModeDevice] = {}
        self.event_bus: mp.Queue[Event] = mp.Queue()  # 进程产出事件
        self.rules = [
            *DEFAULT_RULES,
            SourceRegistered(self.entities),
            TargetRegistered(self.entities),
        ]
        self._mtime = os.path.getmtime(config_path)
        # 内核态设备（well-known 身份，最先托管；事件可寻址，供外部投递）
        self.authority = Authority()
        self.journal = Journal(self.config.get("journal", {}).get("path", "journal.db"))
        self.entities["authority"] = self.authority
        self.entities["journal"] = self.journal
        self._kdispatchers: dict[str, BucketDispatcher] = {}
        for device in (self.authority, self.journal):
            self._kdispatchers[device.identity] = BucketDispatcher(
                device.respond,
                lambda event, d=device: self._kernel_emit(d, event),
                0,
            )

    async def _kernel_emit(self, device, event: Event):
        """内核态设备产出：source 由宿主注入（对应 Emitter 对用户态的角色）。"""
        event["source"] = device.identity
        await self._process_event(event)

    @staticmethod
    def _load(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    # ------------------------------------------------------------------
    # 注册（配置驱动）
    # ------------------------------------------------------------------

    async def setup(self):
        """配置驱动注册：设备/agent（声明工具定义）→ 注入工具条目。"""
        for spec in self.config.get("devices", []) + self.config.get("agents", []):
            identity = spec["identity"]
            device_cls = DEVICE_TYPES[spec["type"]]
            options = spec.get("options", {})
            await self.register(
                identity,
                lambda emit, cls=device_cls, o=options: cls(emit, **o),
                tools=spec.get("tools", []),
                agent=spec["type"] == "agent",
            )
        for agent in self.config.get("agents", []):
            await self._inject(agent["identity"])

    async def register(self, identity, spawn, *, tools=None, agent=False, lazy=False):
        """注册进程：Authority 裁决（身份 + 能力声明）→ kernel 物化路由映射。"""
        await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "register_request", "identity": identity,
                        "tools": tools or [], "agent": agent},
        })
        handle = ProcessHandle(identity, spawn, event_bus=self.event_bus, lazy=lazy)
        self.entities[identity] = handle

    async def _inject(self, agent: str):
        """请求 Authority 构造注入事件并路由给 agent（系统工具一条路径）。"""
        inject = await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "inject_request", "agent": agent},
        })
        if inject != VOID:
            await self._kernel_emit(self.authority, inject)

    # ------------------------------------------------------------------
    # 统一处理路径：校验 → 记录 → 路由
    # ------------------------------------------------------------------

    async def _process_event(self, event: Event, *, record: bool = True):
        """总线到达与内核态设备产出共用：校验 → 记录 → 路由。

        校验失败（非法 / source / target 未注册）响亮丢弃并记录原因——
        print 与 Journal 是同一路径的两面（print 即未来 Journal 的原型）。
        """
        try:
            validate_event(event, self.rules)
        except EventError as err:
            print(f"[protocol] {err}")
            if record:
                await self._record(event, "dropped", str(err))
            return
        if record:
            await self._record(event, "routed", None)
        self._route(event)

    async def _record(self, event: Event, outcome: str, reason: str | None):
        await self.journal.respond({
            "source": "system", "target": "journal", "kind": "system",
            "payload": {"command": "journal_record", "event": event,
                        "outcome": outcome, "reason": reason},
        })

    def _route(self, event: Event):
        target = event["target"]
        entity = self.entities.get(target)
        if isinstance(entity, ProcessHandle):
            entity.deliver(event)
        elif isinstance(entity, KernelModeDevice):
            self._kdispatchers[target].submit(event)
        # else: 校验已保证 target 已注册，理论不可达

    # ------------------------------------------------------------------
    # 事件循环
    # ------------------------------------------------------------------

    def run(self):
        """驱动内核事件循环（阻塞，直至进程终止）。"""
        asyncio.run(self._run())

    async def run_async(self):
        """事件循环（供宿主以 task 方式驱动）。"""
        tick = float(self.config.get("tick", 0.02))
        interval = float(self.config.get("reload_interval", 1.0))
        last_check = time.time()
        while True:
            await self.step()
            if time.time() - last_check >= interval:
                await self.reload_check()
                last_check = time.time()
            await asyncio.sleep(tick)

    async def step(self):
        """一个 tick：取事件 → 校验 → 记录 → 路由。"""
        while not self.event_bus.empty():
            await self._process_event(self.event_bus.get())

    # ------------------------------------------------------------------
    # 热加载
    # ------------------------------------------------------------------

    async def reload_check(self):
        """配置文件变化 → 重新登记能力声明 → 重新注入（工具定义热加载）。

        第一版范围：工具定义的增删改（设备/agent 集合变化不处理）。
        """
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        config = self._load(self.config_path)
        print("[kernel] 配置变更，热加载工具定义")
        for spec in config.get("devices", []):
            await self.authority.respond({
                "source": "system", "target": "authority", "kind": "system",
                "payload": {"command": "register_request", "identity": spec["identity"],
                            "tools": spec.get("tools", []), "agent": False},
            })
        for agent in config.get("agents", []):
            await self._inject(agent["identity"])
