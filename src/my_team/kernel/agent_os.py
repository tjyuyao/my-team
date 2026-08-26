"""Kernel：AgentOS 与事件调度。

统一通信协议：事件 = (source, target, kind, payload)。
- source: 宿主侧（Emitter）注入进程产出事件，值为该进程身份（不可冒充）
- target: 发送方填，决定发给谁（内核按它路由）；必须指向已注册进程
- kind: "system"（内核语义，payload.command）| "application"（业务语义）
- payload: 任意；system 层约定 command（terminate/install_device/uninstall_device）

三态：
- kernel：调度（校验 → 记录 → 路由）+ 托管内核态设备（Authority/Journal），
  自身可寻址（target="kernel"），承接设备的安装/卸载裁决；
- agent/device：进程；用户态 = 真实子进程，内核态 = 与 kernel 同进程。

工作目录驱动：设备不在配置中静态声明——agent（Root）在工作目录生产
设备源码，经 install_device 事件动态装载（源码即持久化形态），经
uninstall_device 热卸载；装载即向 Authority 登记能力并注入全部 agent。
"""

import asyncio
import importlib.util
import multiprocessing as mp
import os
import sys
import uuid

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

# 进程类型注册表：config "type" → 进程类（构造参数经 options）。
# 模块在 import 时自注册（如 my_team.agent 注册 "agent"）；设备不在此列——
# 设备一律由工作目录动态装载，不走类型注册表。
PROCESS_TYPES: dict[str, type] = {}

KERNEL_IDENTITY = "kernel"


class AgentOS:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load(config_path)
        self.entities: dict[str, ProcessHandle | KernelModeDevice | AgentOS] = {}
        self.entities[KERNEL_IDENTITY] = self  # 内核可寻址（install/uninstall）
        self.event_bus: mp.Queue[Event] = mp.Queue()  # 进程产出事件
        self.rules = [
            *DEFAULT_RULES,
            SourceRegistered(self.entities),
            TargetRegistered(self.entities),
        ]
        # 内核态设备（well-known 身份，最先托管；事件可寻址，供外部投递）
        self.authority = Authority()
        self.journal = Journal(self.config.get("journal", {}).get("path", "journal.db"))
        self.entities["authority"] = self.authority
        self.entities["journal"] = self.journal
        self._kdispatchers: dict[str, BucketDispatcher] = {}
        for device in (self.authority, self.journal):
            self._kdispatchers[device.identity] = BucketDispatcher(
                device.respond,
                lambda event, d=device: self._kernel_emit(d.identity, event),
                0,
            )

    async def _kernel_emit(self, source: str, event: Event):
        """内核侧产出事件：source 由内核指定（对应用户态 Emitter 的宿主注入）。"""
        event["source"] = source
        await self._process_event(event)

    @staticmethod
    def _load(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    # ------------------------------------------------------------------
    # 注册（agent 拓扑来自配置）
    # ------------------------------------------------------------------

    async def setup(self):
        """配置驱动注册：agent 拓扑来自配置；设备由工作目录运行期装载。"""
        for spec in self.config.get("agents", []):
            identity = spec["identity"]
            cls = PROCESS_TYPES[spec["type"]]
            options = spec.get("options", {})
            await self.register(
                identity,
                lambda emit, c=cls, o=options: c(emit, **o),
                agent=True,
            )

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
            await self._kernel_emit(self.authority.identity, inject)

    # ------------------------------------------------------------------
    # 设备热装卸（内核裁决：工作目录源码 → 动态装载 → 登记 → 注入）
    # ------------------------------------------------------------------

    async def _on_kernel(self, event: Event):
        """target=kernel 的系统命令（install_device / uninstall_device）。"""
        command = event["payload"].get("command")
        if command == "install_device":
            await self._install(event)
        elif command == "uninstall_device":
            await self._uninstall(event)
        else:
            print(f"[kernel] 未知内核命令: {command!r}")

    async def _install(self, event: Event):
        """装载设备：加载工作目录源码（约定导出 Device 与 TOOLS）→
        注册（Authority 登记 + kernel 物化路由）→ 注入全部 agent。
        任何一步失败都回告请求方（ok=False）——设备源码是用户代码，
        其失败不得击穿内核事件循环；身份类别受保护（内核态/agent 不可
        被设备顶替）。同名已注册设备先终止旧进程（重装即升级）。"""
        payload = event["payload"]
        identity = payload.get("identity")
        try:
            if not isinstance(identity, str) or not identity:
                raise ValueError("install_device 缺 identity")
            old = self.entities.get(identity)
            if old is self or isinstance(old, KernelModeDevice):
                raise ValueError(f"内核态身份不可装卸: {identity!r}")
            agents = await self._agents()
            if identity in agents:
                raise ValueError(f"agent 身份不可被设备顶替: {identity!r}")
            device_cls, tools = self._load_module(identity, payload["source_file"])
            if isinstance(old, ProcessHandle):
                old.terminate()  # join 阻塞内核循环（≤5s），超时后旧进程可能
                # 存活并同身份并存——第一版已知边界
            options = payload.get("options") or {}
            await self.register(identity, lambda emit: device_cls(emit, **options),
                                tools=tools)
            for agent in agents:
                await self._inject(agent)
            ok, error = True, None
        except Exception as exc:
            ok, error = False, str(exc)
        await self._kernel_emit(KERNEL_IDENTITY,
                                self._ack(event, "device_installed", identity,
                                          ok, error))

    async def _uninstall(self, event: Event):
        """卸载设备：终止进程 → Authority 撤销声明 → 重注入（diff 出 evict）。
        失败（未注册/身份类别/异常）一律回告请求方。"""
        payload = event["payload"]
        identity = payload.get("identity")
        try:
            if not isinstance(identity, str) or not identity:
                raise ValueError("uninstall_device 缺 identity")
            old = self.entities.get(identity)
            if old is self or isinstance(old, KernelModeDevice):
                raise ValueError(f"内核态身份不可装卸: {identity!r}")
            agents = await self._agents()
            if identity in agents:
                raise ValueError(f"agent 身份不可装卸: {identity!r}")
            if not isinstance(old, ProcessHandle):
                raise ValueError(f"未注册: {identity!r}")
            old.terminate()
            del self.entities[identity]
            await self.authority.respond({
                "source": "system", "target": "authority", "kind": "system",
                "payload": {"command": "unregister_request", "identity": identity},
            })
            for agent in agents:
                await self._inject(agent)
            ok, error = True, None
        except Exception as exc:
            ok, error = False, str(exc)
        await self._kernel_emit(KERNEL_IDENTITY,
                                self._ack(event, "device_uninstalled", identity,
                                          ok, error))

    @staticmethod
    def _load_module(identity: str, path: str):
        """按文件路径加载设备模块（模块名带随机段：重装不命中旧缓存）。

        依赖 fork 启动方式（Device 实例在父进程构造、经进程继承分发）；
        sys.modules 条目不清理（随机段保证永不冲突）——第一版已知边界。
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"设备源码不存在: {path!r}")
        name = f"team_device_{identity}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module.Device, module.TOOLS

    async def _agents(self) -> list[str]:
        """向 Authority 查询全部 agent 身份（组织事实归 Authority）。"""
        reply = await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "agents_request"},
        })
        return reply["payload"]["agents"]

    @staticmethod
    def _ack(event: Event, command: str, identity: str, ok: bool,
             error: str | None) -> dict:
        return {"target": event["source"], "kind": "application",
                "payload": {"command": command, "identity": identity,
                            "ok": ok, "error": error}}

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
        if event["target"] == KERNEL_IDENTITY:
            await self._on_kernel(event)
        else:
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
        """事件循环（供宿主以 task 方式驱动）。

        热装卸是事件驱动（install/uninstall_device 即时生效），无轮询。
        """
        tick = float(self.config.get("tick", 0.02))
        while True:
            await self.step()
            await asyncio.sleep(tick)

    async def step(self):
        """一个 tick：取事件 → 校验 → 记录 → 路由。"""
        while not self.event_bus.empty():
            await self._process_event(self.event_bus.get())
