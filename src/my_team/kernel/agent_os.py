"""Kernel：AgentOS 与事件调度。

统一通信协议：事件 = (source, target, kind, payload)。
- source: 宿主侧（Emitter）注入进程产出事件，值为该进程 pid
  （uuid 随机性保证不可冒充）
- target: 发送方填，决定发给谁（内核按它路由）
- kind: "system"（内核语义，payload.command）| "application"（业务语义）
- payload: 任意；system 层约定 command（terminate）

事件只来自进程 emit（外部世界经设备进程转接）；宿主不直接发事件。
"""

import multiprocessing as mp
import time
from uuid import uuid4

from my_team.kernel.event_validator import (
    DEFAULT_RULES,
    EventError,
    SourceRegistered,
    validate_event,
)
from my_team.kernel.process_handle import ProcessHandle


class AgentOS:
    def __init__(self):
        self.processes = {}   # pid → ProcessHandle（agent/device 统一）
        self.event_bus = mp.Queue()   # 进程产出事件
        self.rules = [*DEFAULT_RULES, SourceRegistered(self.processes)]

    def register(self, spawn, lazy=False):
        pid = None
        while pid is None or pid in self.processes:
            pid = str(uuid4())
        handle = ProcessHandle(pid, spawn, event_bus=self.event_bus, lazy=lazy)
        self.processes[handle.pid] = handle

    def route(self, event):
        """路由事件到目标进程句柄（进 inbox，惰性拉起）。"""
        handle = self.processes.get(event["target"])
        if handle is None:
            return None
        return handle.deliver(event)

    def step(self):
        """一个 tick：取事件 → 协议校验 → 路由。

        事件只来自进程 emit；协议非法或 source 未注册的事件一律丢弃，
        不中断内核循环。
        """
        while not self.event_bus.empty():
            e = self.event_bus.get()
            try:
                validate_event(e, self.rules)
            except EventError as e:
                print(e)
                continue
            self.route(e)

    def run(self, n, tick_duration=0.1):
        """跑 n 个 tick，tick 之间 sleep（tick_duration 秒）。"""
        for _ in range(n):
            self.step()
            time.sleep(tick_duration)
