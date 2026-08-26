"""Kernel：Process 抽象（协议）与两态实现。

Process = 一套契约：``async respond(event) -> Event | VOID``。
- 返回事件 → emit（产出，source 由宿主注入）；
- 返回 ``VOID``（"VOID" 哨兵）→ 合法沉默，不上总线；
- 返回 None → 协议违规，响亮丢弃（None 无法区分"故意沉默"与"忘记返回"）。

两态同构（接口完全兼容，传输是唯一差异）：
- 用户态 ``UserModeProcess``：真实子进程（mp.Process），inbox 经 mp.Queue；
- 内核态 ``KernelModeDevice``：与 kernel 同进程托管（可信系统服务，
  well-known 身份，respond 抛错 = kernel 失败）。

共用 ``BucketDispatcher``：按 source 分桶——同源串行保序、跨源并行。
"""

import asyncio
import multiprocessing as mp
from typing import Protocol

from .event_protocol import VOID, Event


class Process(Protocol):
    """Process 契约：respond 处理事件，产出事件或 VOID。"""

    async def respond(self, event: Event) -> Event | VOID: ...


class BucketDispatcher:
    """按 source 分桶的事件分发器（传输无关）。

    - 同源：同一 source 的事件排队串行（FIFO 保序）；
    - 跨源：不同 source 并行（各自 worker）；
    - max_concurrent_sources：同时服务的 source 数上限；0 = 无限
      （超限排队等空位，事件已在 queue 缓冲不丢）。
    - respond 返回 None 属协议违规：响亮丢弃，不阻断该 source。
    - emit 为 async 可调用（用户态=入总线，内核态=process_event）。
    """

    def __init__(self, respond, emit, max_concurrent_sources):
        self.respond = respond
        self.emit = emit
        self.max_concurrent_sources = max_concurrent_sources
        self._queues: dict[str, asyncio.Queue] = {}
        self._running: set[str] = set()
        self._sem: asyncio.Semaphore | None = None

    async def _semaphore(self) -> asyncio.Semaphore | None:
        # 惰性创建：必须在事件循环内（worker 调用时）
        if self._sem is None and self.max_concurrent_sources > 0:
            self._sem = asyncio.Semaphore(self.max_concurrent_sources)
        return self._sem

    def submit(self, event: Event):
        """入桶：同源排队；新 source 起 worker。"""
        source = event["source"]
        queue = self._queues.setdefault(source, asyncio.Queue())
        queue.put_nowait(event)
        if source not in self._running:
            self._running.add(source)
            asyncio.create_task(self._worker(source, queue))

    async def _worker(self, source: str, queue: asyncio.Queue):
        sem = None
        if self.max_concurrent_sources > 0:
            sem = await self._semaphore()
            assert sem is not None
            await sem.acquire()  # 满则等待（事件已在 queue 缓冲，不丢）
        try:
            while True:
                event = await queue.get()
                result = await self.respond(event)
                if result is None:
                    # 协议违规：None 无法区分"故意沉默"与"忘记返回"，响亮丢弃
                    print(f"[protocol] respond 返回 None（source={source}），丢弃")
                    continue
                if result != VOID:
                    await self.emit(result)
                if queue.empty():
                    return
        finally:
            self._running.discard(source)
            if sem is not None:
                sem.release()


class UserModeProcess(mp.Process):
    """用户态进程：真实子进程，inbox 经 mp.Queue，同源串行/跨源并行。

    终止 = system 层 payload.command=="terminate" 的事件。
    动态装载设备（load_spec 非空）：实例在子进程内构造——传输的只是
    可 pickle 的装载描述（identity/path/options），不是类对象，故
    spawn/fork 皆可；装载失败 = 进程崩溃（stderr 响亮），属进程级
    故障而非内核裁决。
    """

    def __init__(self, emit, max_concurrent_sources, *, load_spec=None):
        super().__init__()
        self.inbox: mp.Queue[Event] = mp.Queue()  # 宿主 → 进程
        self.emit = emit  # 进程 → 宿主（Emitter，source 由宿主注入）
        self.max_concurrent_sources = max_concurrent_sources
        self._load_spec = load_spec  # (identity, module_path, options) | None

    async def respond(self, event: Event) -> Event | VOID:
        """处理单个事件，返回产出事件或 VOID（子类实现）。"""
        raise NotImplementedError

    def run(self):
        """子进程主循环：动态装载设备先自举（子进程内加载并构造实例），
        再按 source 分桶 serve。"""
        if self._load_spec is not None:
            self._run_loaded()
        else:
            asyncio.run(self._serve())

    def _run_loaded(self):
        """子进程内装载设备：importlib 加载工作目录源码，构造 Device 实例
        并接管宿主投递的 inbox 后由其自身 serve。"""
        import importlib.util
        import sys
        import traceback
        import uuid

        identity, path, options = self._load_spec
        try:
            name = f"team_device_{identity}_{uuid.uuid4().hex[:8]}"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            device = module.Device(self.emit, **options)
            device.inbox = self.inbox  # 复用宿主投递的队列（其自身新建的弃用）
            asyncio.run(device._serve())
        except Exception:
            traceback.print_exc()
            raise SystemExit(1)

    async def _serve(self):
        dispatcher = BucketDispatcher(self.respond, self._emit, self.max_concurrent_sources)
        while True:
            event = await asyncio.to_thread(self.inbox.get)
            if event.get("kind") == "system" and \
                    event.get("payload", {}).get("command") == "terminate":
                break
            dispatcher.submit(event)

    async def _emit(self, event: Event):
        """产出事件：入总线（source 由 Emitter 宿主侧注入）。"""
        self.emit(event)


class KernelModeDevice:
    """内核态设备：与 kernel 同进程托管。

    特殊地位：可信系统服务（Authority/Journal），well-known 身份；
    无独立进程与生命周期，与 kernel 同生共死。respond 抛错 = kernel
    失败（fail-fast，不允许带病运行）。respond 契约与用户态完全一致。
    """

    def __init__(self, identity: str):
        self.identity = identity

    async def respond(self, event: Event) -> Event | VOID:
        raise NotImplementedError
