"""Kernel：ProcessHandle 统一接口（宿主侧代理，经事件总线通信）。

惰性启动可配置：lazy=True 时首次 deliver 才 spawn 真进程；
lazy=False 时注册即 spawn（启动慢但首个事件零延迟）。

终止契约：``await terminate(timeout)`` 返回时进程必已死亡——先投
system 层 terminate 事件（进程自我收尾），异步等待（不阻塞内核事件
循环），超时强杀（SIGKILL 不可阻塞）后收尸。绝不同身份并存：宿主
仅以 identity 区分进程，同 identity 双活即协议污染。
"""

import asyncio

from .event_protocol import Event
from .process import UserModeProcess


class Emitter:
    """可跨进程 pickle 的产出器：捕获身份与事件总线队列（宿主侧注入 source）。"""

    def __init__(self, identity, event_bus):
        self.identity = identity
        self.event_bus = event_bus

    def __call__(self, event):
        event["source"] = self.identity
        self.event_bus.put(event)


class ProcessHandle:
    """用户态进程的宿主代理：identity → 真实子进程（身份不可冒充）。"""

    def __init__(self, identity, spawn, event_bus, lazy=False):
        self.identity = identity
        self.spawn = spawn
        self.emit = Emitter(identity, event_bus)
        self._process: UserModeProcess | None = None
        if not lazy:
            self._ensure_process()

    def _ensure_process(self) -> UserModeProcess:
        if self._process is None:
            self._process = self.spawn(self.emit)
            self._process.start()  # 拉起真子进程
        return self._process

    def deliver(self, event: Event):
        """投递事件（进 inbox，惰性拉起）。"""
        return self._ensure_process().inbox.put(event)

    async def terminate(self, timeout: float):
        """终止进程并确保其死亡（异步，不阻塞内核事件循环）。

        投 terminate 事件 → 等进程自行退出 → 超时强杀（kill）→ 收尸。
        """
        if self._process is None:
            return
        self.deliver({
            "source": "system", "target": self.identity,
            "kind": "system", "payload": {"command": "terminate"},
        })
        await asyncio.to_thread(self._process.join, timeout)
        if self._process.is_alive():
            self._process.kill()
            await asyncio.to_thread(self._process.join)
        self._process = None
