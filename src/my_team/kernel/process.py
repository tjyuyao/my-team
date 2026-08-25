"""Kernel：Process 本体（真实 multiprocessing 进程）。

子进程自循环：inbox 收事件 → respond() 响应 → emit 回事件总线。
宿主不直接持有进程对象，经 ProcessHandle 通信。

并发模型：
- Agent 串行处理消息；设备并行处理消息。
- 设备按 source 分桶：同一来源的事件排队串行（保序），
  不同来源的事件并行（各自 worker）。
- respond 契约：async，返回产出事件；source 由宿主注入。

与 ProcessHandle 的关系：
- ProcessHandle = 宿主持有的代理
- Process = 真实子进程（自己的状态与循环）
"""

import asyncio
import multiprocessing as mp

from .event_protocol import Event


class Process(mp.Process):
    def __init__(self, emit, max_concurrent_sources):
        super().__init__()
        self.inbox: mp.Queue[Event] = mp.Queue()   # 宿主 → 进程
        self.emit = emit  # 进程 → 宿主
        self.max_concurrent_sources = max_concurrent_sources  # 同时服务的 source 数上限；0 = 无限

    async def respond(self, event: Event):
        """处理单个事件，返回产出事件（子类实现）。

        契约：必须返回合法事件（source 由宿主注入）；返回 None 属协议违规。
        """
        raise NotImplementedError

    def run(self):
        """子进程主循环：asyncio 驱动，按 source 分桶并行处理。

        终止 = system 层 payload.command=="terminate" 的事件。
        """
        asyncio.run(self._serve())

    async def _serve(self):
        queues: dict[str, asyncio.Queue] = {}   # source → 事件队列
        running: set[str] = set()               # 有 worker 在跑的 source
        sem = (
            None
            if self.max_concurrent_sources == 0
            else asyncio.Semaphore(self.max_concurrent_sources)
        )
        while True:
            event = await asyncio.to_thread(self.inbox.get)
            if event.get("kind") == "system" and \
                    event.get("payload", {}).get("command") == "terminate":
                break
            source = event["source"]
            queue = queues.setdefault(source, asyncio.Queue())
            queue.put_nowait(event)
            if source not in running:
                running.add(source)
                asyncio.create_task(
                    self._start_worker(source, queue, running, sem)
                )

    async def _start_worker(self, source: str, queue: asyncio.Queue,
                            running: set[str], sem: asyncio.Semaphore | None):
        """起一个 source 的 worker；并发 source 数超限时排队等空位。"""
        if self.max_concurrent_sources > 0:
            assert sem is not None
            await sem.acquire()  # 满则等待（事件已在 queue 缓冲，不丢）
        try:
            await self._source_worker(source, queue, running)
        finally:
            running.discard(source)
            if sem is not None:
                sem.release()

    async def _source_worker(self, source: str, queue: asyncio.Queue, running: set[str]):
        """同源串行：一个 source 同一时刻只处理一个事件，完成后取下一个。"""
        while True:
            event = await queue.get()
            self.emit(await self.respond(event))
            if queue.empty():
                return
