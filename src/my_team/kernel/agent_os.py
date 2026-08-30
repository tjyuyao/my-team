"""Kernel：AgentOS 与事件调度。

统一通信协议：事件 = (source, target, kind, payload)。
- source: 宿主读侧盖章（reader 按出站队列归属注入）进程产出事件，值为
  该进程身份（进程内不存在可改写身份字段）
- target: 发送方填，决定发给谁（内核按它路由）；必须指向已注册进程
- kind: "system"（内核语义，payload.command）| "application"（业务语义）
- payload: 任意；system 层约定 command（terminate/install_device/uninstall_device）

三态：
- kernel：调度（校验 → 记录 → 路由）+ 托管内核态设备（Authority/Journal），
  自身可寻址（target="kernel"），承接设备的安装/卸载裁决；
- agent/device：进程；用户态 = 真实子进程，内核态 = 与 kernel 同进程。

设备是普通身份，代码与状态都位于该身份的私有 home。install_device
从 identity 私家 ``data/<identity>/device.py`` 装载设备实现。
"""

import asyncio
import importlib.util
import multiprocessing as mp
import os
import shutil
import sys
import uuid

import yaml

from my_team.kernel.authority import ORG_DEVICE, Authority
from my_team.kernel.event_protocol import VOID, Event
from my_team.kernel.event_validator import (
    DEFAULT_RULES,
    EventError,
    SourceRegistered,
    TargetRegistered,
    validate_event,
)
from my_team.kernel.journal import Journal
from my_team.kernel.process import (
    BucketDispatcher,
    KernelModeDevice,
    UserModeProcess,
)
from my_team.kernel.process_handle import ProcessHandle

# 进程类型注册表：config "type" → 进程类（构造参数经 options）。
# 模块在 import 时自注册（如 my_team.agent 注册 "agent"）；设备不在此列——
# 设备一律由工作目录动态装载，不走类型注册表。
PROCESS_TYPES: dict[str, type] = {}

KERNEL_IDENTITY = "kernel"

# 内核态来源（kernel/authority/journal）的认证上下文：无 position、无 scopes
EMPTY_AUTH: dict = {"position": None, "scopes": []}


class AgentOS:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load(config_path)
        self.runtime_root = os.path.realpath(self.config["runtime_root"])
        # 拒绝源码树作为 runtime_root：检测 src/my_team/ 标志目录
        src_marker = os.path.join(self.runtime_root, "src", "my_team")
        if os.path.isdir(src_marker):
            raise ValueError(
                f"runtime_root 不能是源码树: {self.runtime_root!r}。"
                f"请使用空目录或临时目录。")
        os.makedirs(self.runtime_root, exist_ok=True)
        self.entities: dict[str, ProcessHandle | KernelModeDevice | AgentOS] = {}
        self.entities[KERNEL_IDENTITY] = self  # 内核可寻址（install/uninstall）
        self.event_bus: mp.Queue[Event] = mp.Queue()  # 进程产出事件
        self._agent_ids: set[str] = set()  # agent 身份（路由富化跳过目标）
        self.rules = [
            *DEFAULT_RULES,
            SourceRegistered(self.entities),
            TargetRegistered(self.entities),
        ]
        # 内核态设备（well-known 身份，最先托管；事件可寻址，供外部投递）
        self.authority = Authority("authority", self.runtime_root)
        journal_path = os.path.join(self.runtime_root, "home", "journal",
                                    "journal.db")
        self.journal = Journal("journal", self.runtime_root, journal_path)
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
        """内核侧产出事件：source 由内核指定（对应用户态的宿主读侧盖章）。"""
        event["source"] = source
        await self._process_event(event)

    @staticmethod
    def _load(path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # 注册（agent 拓扑来自配置）
    # ------------------------------------------------------------------

    async def setup(self):
        """配置驱动注册：agent 拓扑来自配置；设备从 config.devices 声明安装。"""
        for spec in self.config.get("agents", []):
            identity = spec["identity"]
            cls = PROCESS_TYPES[spec["type"]]
            options = spec.get("options", {})
            await self.register(
                identity,
                lambda emit, c=cls, o=options, i=identity: c(
                    emit, runtime_root=self.runtime_root, identity=i, **o),
                agent=True,
                position=options.get("position"),
            )
            # agent 家 = runtime_root/home/<agent-id>：注册时创建（幂等）
            os.makedirs(os.path.join(self.runtime_root, "home", identity),
                        exist_ok=True)
        # 设备从 config.devices 声明安装：复制源码到设备家 → install_device
        for spec in self.config.get("devices", []):
            identity = spec["identity"]
            source = spec["source"]
            if not os.path.isabs(source):
                # 相对于配置文件所在目录
                source = os.path.realpath(os.path.join(
                    os.path.dirname(self.config_path), source))
            device_home = self._device_home(identity)
            os.makedirs(device_home, exist_ok=True)
            dest = os.path.join(device_home, "device.py")
            shutil.copy2(source, dest)
            grants = spec.get("grants", [])
            await self._install_from_config(identity, grants)

    async def register(self, identity, spawn, *, tools=None, agent=False,
                       lazy=False, position=None, scopes=None):
        """注册进程：Authority 裁决（身份 + 能力声明 + position + scope
        声明）→ kernel 物化路由映射。agent 必须声明 position（布线主体，
        config options 单一来源），缺省即配错，fail-fast。身份统一校验
        （拒路径分隔符、'.'、'..' 与 '@'）。"""
        if agent and not position:
            raise ValueError(f"agent 缺少 position: {identity!r}")
        if "/" in identity or ".." in identity or identity == "." \
                or "@" in identity:
            raise ValueError(
                f"身份非法（拒路径分隔符/'..'/'.'与'@'): "
                f"{identity!r}")
        await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "register_request", "identity": identity,
                        "tools": tools or [], "agent": agent,
                        "position": position, "scopes": scopes or []},
        })
        handle = ProcessHandle(identity, spawn, event_bus=self.event_bus, lazy=lazy)
        self.entities[identity] = handle
        if agent:
            self._agent_ids.add(identity)

    async def _install_from_config(self, identity: str, grants: list):
        """从 config 声明安装设备（setup 阶段，kernel 隐式授权）。"""
        device_home = self._device_home(identity)
        source_file = os.path.join(device_home, "device.py")
        tools, scopes = self._load_module(identity, source_file)
        await self.register(
            identity,
            lambda emit, p=source_file, i=identity: UserModeProcess(
                emit, 0, self.runtime_root, load_spec=(i, p, {})),
            tools=tools, scopes=scopes)
        for position in grants:
            for token in self._defaults(scopes):
                await self._grant(position, identity, token)
        for agent in await self._agents():
            await self._inject(agent)

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
        """target=kernel 的系统命令（install/uninstall_device、
        grant/revoke_scope、maintenance_session）。"""
        command = event["payload"].get("command")
        if command == "install_device":
            await self._install(event)
        elif command == "uninstall_device":
            await self._uninstall(event)
        elif command == "maintenance_session":
            await self._maintenance_session(event)
        elif command in ("grant_scope", "revoke_scope"):
            await self._scope(event, grant=command == "grant_scope")
        else:
            print(f"[kernel] 未知内核命令: {command!r}")

    async def _authorize(self, identity: str, scope: str) -> bool:
        """经 Authority 裁决系统命令权（root 或持 org scope）。"""
        reply = await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "authorize_request",
                        "identity": identity, "scope": scope}})
        return reply["payload"]["allowed"]  # type: ignore[no-any-return]

    async def _authorize_device(self, actor: str, device: str) -> bool:
        """Allow root/org installers, kernel, or a maintainer explicitly scoped to device."""
        # Kernel identity has implicit install authority
        if actor == KERNEL_IDENTITY:
            return True
        if await self._authorize(actor, "install"):
            return True
        auth = await self._auth_context(actor)
        return any(
            grant.get("device") == device
            and grant.get("token") in {"maintain", f"maintain:{device}"}
            for grant in auth.get("scopes", [])
        )

    async def _auth_context(self, identity: str) -> dict:
        """调用时认证上下文（富化用）：position + 有效 scopes。"""
        reply = await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "auth_request", "identity": identity}})
        return reply["payload"]["auth"]  # type: ignore[no-any-return]

    @staticmethod
    def _defaults(scopes: list) -> list[str]:
        """设备声明的默认公开 scope（安装布线展开用）。"""
        return [s["token"] for s in scopes if s.get("default")]

    async def _install(self, event: Event):
        """装载设备私有 home 中的 ``device.py``（导出 Device、TOOLS，可选
        SCOPES）→ 注册并布线 → 注入全部 agent。装卸权经 Authority
        裁决（root 或持有 org:install）。任何一步失败都回告请求方（ok=False）
        设备加载失败时回告调用方；设备 identity 拥有一个私家和一个运行实例。
        重装先终止现有实例，维护者可在卸载后编辑私家再重新装载。
        """
        payload = event["payload"]
        identity = payload.get("identity")
        instance_identity = identity
        try:
            if not isinstance(identity, str) or not identity:
                raise ValueError("install_device 缺 identity")
            if "/" in identity or ".." in identity or identity == "." \
                    or "@" in identity:
                raise ValueError(
                    f"设备 identity 非法（拒路径分隔符/'..'/'.'与'@'): {identity!r}")
            device_home = self._device_home(identity)
            source_file = os.path.join(device_home, "device.py")
            if not await self._authorize_device(event["source"], identity):
                raise ValueError("无该设备维护权（需 root/org:install 或 maintain scope）")
            grants = payload.get("grants")
            if not isinstance(grants, list) or not grants or not all(
                    isinstance(g, str) and g for g in grants):
                raise ValueError(
                    "install_device 缺 grants（布线声明：非空 position 列表）")
            agents = await self._agents()
            if identity in agents:
                raise ValueError(f"agent 身份不可被设备顶替: {identity!r}")
            tools, scopes = self._load_module(identity, source_file)
            instance_identity = identity
            # Device home is its private persistence boundary.
            os.makedirs(os.path.join(self.runtime_root, "home", identity),
                        exist_ok=True)
            old = self.entities.get(instance_identity)
            if old is self or isinstance(old, KernelModeDevice):
                raise ValueError(f"内核态身份不可装卸: {instance_identity!r}")
            if isinstance(old, ProcessHandle):
                # 先摘身份再终止：终止等待期间路由到该身份的事件被校验
                # 响亮丢弃（target 未注册），旧进程残余产出被拒绝——
                # 绝不复活同身份进程（绝不同身份并存）
                del self.entities[instance_identity]
                await old.terminate(5)
                # 撤销旧登记的 grants（重装即升级：旧 scope 不得残留）
                await self.authority.respond({
                    "source": "system", "target": "authority", "kind": "system",
                    "payload": {"command": "unregister_request",
                                "identity": instance_identity}})
            options = payload.get("options") or {}
            # 壳进程只携带装载描述；Device 实例在子进程内构造
            # （沙箱 re-entry：sandbox_entry._serve_device），spawn/fork 皆可
            await self.register(
                instance_identity,
                lambda emit, p=source_file, o=options,
                i=instance_identity: UserModeProcess(
                    emit, o.get("max_concurrent_sources", 0),
                    self.runtime_root, load_spec=(i, p, o)),
                tools=tools, scopes=scopes)
            for position in grants:
                for token in self._defaults(scopes):
                    await self._grant(position, instance_identity, token)
            for agent in agents:
                await self._inject(agent)
            ok, error = True, None
        except Exception as exc:
            ok, error = False, str(exc)
        await self._kernel_emit(KERNEL_IDENTITY,
                                self._ack(event, "device_installed",
                                          instance_identity, ok, error))

    async def _uninstall(self, event: Event):
        """卸载设备：终止进程 → Authority 撤销声明（连带撤销其全部布线）
        → 重注入（diff 出 evict）。装卸权经 Authority 裁决；失败（未注册/
        身份类别/异常）一律回告请求方。"""
        payload = event["payload"]
        identity = payload.get("identity")
        try:
            if not isinstance(identity, str) or not identity:
                raise ValueError("uninstall_device 缺 identity")
            if not await self._authorize_device(event["source"], identity):
                raise ValueError("无该设备维护权（需 root/org:install 或 maintain scope）")
            old = self.entities.get(identity)
            if old is self or isinstance(old, KernelModeDevice):
                raise ValueError(f"内核态身份不可装卸: {identity!r}")
            agents = await self._agents()
            if identity in agents:
                raise ValueError(f"agent 身份不可装卸: {identity!r}")
            if not isinstance(old, ProcessHandle):
                raise ValueError(f"未注册: {identity!r}")
            # 先摘身份再终止（同 _install：终止期间路由/产出均被校验拒绝）
            del self.entities[identity]
            await old.terminate(5)  # 确保进程死亡（超时强杀），绝不同身份并存
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


    async def _maintenance_session(self, event: Event):
        """维护会话入口：对已卸载设备执行维护操作（编辑私家实现）。

        维护者必须持有 target 设备的 maintain scope（经 _authorize_device 裁决）。
        目标设备必须已卸载（未注册），否则拒绝。
        维护会话的挂载范围只含维护者自身家与目标设备家（双锚点可写）。
        维护者身份不能是设备（不能维护自己的设备）。
        """
        payload = event["payload"]
        target = payload.get("target_device")
        actor = event["source"]

        try:
            # 参数校验
            if not isinstance(target, str) or not target:
                raise ValueError("maintenance_session 缺 target_device")
            if "/" in target or ".." in target or target == "." \
                    or "@" in target:
                raise ValueError(f"维护目标 identity 非法: {target!r}")

            # 维护者不能是设备（不能维护自己的设备）
            actor_entity = self.entities.get(actor)
            if isinstance(actor_entity, ProcessHandle) and \
                    actor_entity._load_spec is not None:
                raise ValueError(f"设备身份不可作为维护者: {actor!r}")

            # 权限裁决：维护者必须持有目标设备的 maintain scope
            if not await self._authorize_device(actor, target):
                raise ValueError(f"无目标设备维护权: {target!r}")

            # 目标设备必须已卸载（未注册）
            if target in self.entities:
                raise ValueError(f"目标设备仍在运行，必须先卸载: {target!r}")

            # 目标设备必须存在（有私家目录）
            target_home = os.path.join(self.runtime_root, "home", target)
            if not os.path.exists(target_home):
                raise ValueError(f"目标设备私家不存在: {target_home!r}")

            # 构造维护会话进程：维护者身份 + 目标设备私家可写
            # 注意：维护者必须是已注册的 agent（有 workdir）
            actor_handle = self.entities.get(actor)
            if not isinstance(actor_handle, ProcessHandle):
                raise ValueError(f"维护者必须是已注册的 agent: {actor!r}")

            # 终止现有维护者进程（如果有）
            old = self.entities.get(actor)
            if isinstance(old, ProcessHandle):
                del self.entities[actor]
                await old.terminate(5)

            # 注册维护会话进程：使用维护者身份，但设置 maintenance_device
            # 这样 _mount_anchors() 会返回双锚点（维护者家 + 目标设备家）
            async def spawn_maintenance(emit):
                process = UserModeProcess(
                    emit, 0, self.runtime_root,
                    identity=actor, maintenance_device=target)
                return process

            await self.register(
                actor,
                spawn_maintenance,
                agent=True,
                position=actor_handle._position if hasattr(actor_handle, '_position') else None,
            )

            ok, error = True, None
        except Exception as exc:
            ok, error = False, str(exc)

        await self._kernel_emit(KERNEL_IDENTITY,
                                self._ack(event, "maintenance_session_started",
                                          target if ok else actor, ok, error))

    async def _scope(self, event: Event, *, grant: bool):
        """运行期 grant/revoke_scope（人事权操作）：经 Authority 裁决 →
        登记/撤销 (position, device, token) → 重注入全部 agent → ack。
        org 设备（人事权本身）的授予仅 root 可做。"""
        payload = event["payload"]
        position, device, token = (payload.get("position"),
                                   payload.get("device"), payload.get("token"))
        try:
            if not (isinstance(position, str) and position
                    and isinstance(device, str) and device
                    and isinstance(token, str) and token):
                raise ValueError("grant/revoke_scope 缺 position/device/token")
            if device == ORG_DEVICE and not await self._authorize(
                    event["source"], "org"):
                raise ValueError("org 设备授权仅 root（或其委托）可做")
            if not await self._authorize(event["source"], "grant"):
                raise ValueError("无人事权（需 root 或 org:grant）")
            await self.authority.respond({
                "source": "system", "target": "authority", "kind": "system",
                "payload": {"command": "grant_request" if grant
                            else "revoke_request",
                            "position": position, "device": device,
                            "token": token}})
            for agent in await self._agents():
                await self._inject(agent)
            ok, error = True, None
        except Exception as exc:
            ok, error = False, str(exc)
        await self._kernel_emit(KERNEL_IDENTITY, {
            "target": event["source"], "kind": "application",
            "payload": {"command": "scope_granted" if grant
                        else "scope_revoked",
                        "position": position, "device": device,
                        "ok": ok, "error": error}})

    async def _grant(self, position: str, device: str, token: str):
        """Authority 布线登记（kernel 内部：装卸展开与运行期共用一个入口）。"""
        await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "grant_request",
                        "position": position, "device": device,
                        "token": token}})

    def _device_home(self, identity: str) -> str:
        """设备私有 home：runtime_root/home/<identity>/。"""
        return os.path.join(self.runtime_root, "home", identity)

    @staticmethod
    def _load_module(identity: str, path: str) -> tuple[list, list]:
        """校验并读取设备源码定义（可选 TOOLS、SCOPES）。

        仅父进程裁决用（模块可加载、定义齐全）；设备实例在子进程内构造
        （传输无关，spawn/fork 皆可）。TOOLS 缺省为空列表（服务型设备，
        如 bash、llm 不声明工具，由 agent 直接发事件调用）。
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"设备源码不存在: {path!r}")
        name = f"team_device_{identity}_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise FileNotFoundError(f"设备模块 spec 解析失败: {path!r}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        tools = getattr(module, "TOOLS", [])
        if not isinstance(tools, list):
            raise ValueError(f"TOOLS 声明形状非法: {path!r}")
        scopes = getattr(module, "SCOPES", [])
        if not isinstance(scopes, list) or not all(
                isinstance(s, dict) and isinstance(s.get("token"), str)
                and s["token"] for s in scopes):
            raise ValueError(f"SCOPES 声明形状非法: {path!r}")
        return tools, scopes

    async def _agents(self) -> list[str]:
        """向 Authority 查询全部 agent 身份（组织事实归 Authority）。"""
        reply = await self.authority.respond({
            "source": "system", "target": "authority", "kind": "system",
            "payload": {"command": "agents_request"},
        })
        return reply["payload"]["agents"]  # type: ignore[no-any-return]

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
        if event["target"] != KERNEL_IDENTITY:
            # 路由前富化：认证上下文进 Journal 的"内核所见"
            await self._enrich(event)
        if record:
            await self._record(event, "routed", None)
        if event["target"] == KERNEL_IDENTITY:
            await self._on_kernel(event)
        else:
            self._route(event)

    async def _enrich(self, event: Event):
        """调用时认证（富化）：路由到设备（非 agent）的事件附加调用者的
        (position, scopes)——宿主侧解析无伪造面，设备按自己的语义裁决。
        经 Authority 进程内直调，零 IPC。内核态来源无认证上下文。"""
        if event["target"] in self._agent_ids:
            return
        entity = self.entities.get(event["target"])
        if not isinstance(entity, ProcessHandle):
            return
        source = event["source"]
        source_entity = self.entities.get(source)
        if source == KERNEL_IDENTITY or \
                isinstance(source_entity, KernelModeDevice):
            event["auth"] = EMPTY_AUTH
            return
        event["auth"] = await self._auth_context(source)

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
