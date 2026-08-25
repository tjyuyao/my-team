"""Kernel：Process 本体（真实 multiprocessing 进程）。

子进程自循环：inbox 收事件 → respond() 响应 → emit 回事件总线。
宿主不直接持有进程对象，经 ProcessHandle 通信。

与 ProcessHandle 的关系：
- ProcessHandle = 宿主持有的代理
- Process = 真实子进程（自己的状态与循环）
"""

import multiprocessing as mp

from .event_protocol import Event


class Process(mp.Process):
    def __init__(self, emit, poll_interval=0.05):
        super().__init__()
        self.inbox: mp.Queue[Event] = mp.Queue()   # 宿主 → 进程
        self.emit = emit  # 进程 → 宿主
        self.poll_interval = poll_interval

    def respond(self, event: Event):
        """处理单个事件，返回结果（子类实现）。"""
        raise NotImplementedError

    def run(self):
        """子进程主循环：收事件 → 处理 → emit 结果（间隔 poll_interval）。

        终止 = system 层 payload.command=="terminate" 的事件。
        """
        while True:
            try:
                event: Event = self.inbox.get(timeout=self.poll_interval)
            except Exception:      # 超时无事件 → 继续等
                continue
            if event.get("kind") == "system" and \
                    event.get("payload", {}).get("command") == "terminate":
                break
            self.emit(self.respond(event))

