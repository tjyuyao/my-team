"""Kernel：ProcessHandle 统一接口（宿主侧代理，经事件总线通信）。

惰性启动可配置：lazy=True 时首次 deliver 才 spawn 真进程；
lazy=False 时注册即 spawn（启动慢但首个事件零延迟）。

传输：每进程一条 socketpair（fd 继承；mp.Queue 依赖 /dev/shm 命名信号量，
同 uid 可枚举/干扰，与沙箱隔离承诺冲突，弃用）。宿主持 parent 端、
child 端随 spawn 传入进程，两端各包 ``multiprocessing.connection.Connection``
（自带 pickle 帧协议）。

出站（宿主读侧盖章）：子进程产出（child 端 send）不含 source——进程内
不存在可改写身份字段；常驻 daemon reader 线程按队列归属盖章后入
event_bus（EOF 即进程死亡，线程退出）。宿主直投（event_bus.put 显式
source）保留。

终止契约：``await terminate(timeout)`` 返回时进程必已死亡——先投
system 层 terminate 事件（进程自我收尾），异步等待（不阻塞内核事件
循环），超时强杀（SIGKILL 不可阻塞）后收尸。绝不同身份并存：宿主
仅以 identity 区分进程，同 identity 双活即协议污染。
"""

import asyncio
import socket
import threading
from multiprocessing.connection import Connection

from .event_protocol import Event
from .process import UserModeProcess


class ChildWriter:
    """子进程内产出写入器：写 child 端 Connection（无身份字段）。

    可 pickle 的小对象（Connection 有 reduction 支持，spawn 管道传 socket
    fd）；事件不含 source——宿主 reader 读侧盖章，进程内无可改写身份。
    """

    def __init__(self, conn: Connection):
        self.conn = conn

    def __call__(self, event):
        self.conn.send(event)


class ProcessHandle:
    """用户态进程的宿主代理：identity → 真实子进程（身份不可冒充）。"""

    def __init__(self, identity, spawn, event_bus, lazy=False):
        self.identity = identity
        self.spawn = spawn
        self.event_bus = event_bus
        self._process: UserModeProcess | None = None
        self._reader: threading.Thread | None = None
        if not lazy:
            self._ensure_process()

    def _open_channel(self):
        """每进程一条 socketpair：宿主持 parent 端（投递/读侧盖章），
        child 端随 spawn 传入进程。socket 对象必须保持引用（Connection
        只持 fd 整数，socket __del__ 会关 fd）。"""
        parent_sock, child_sock = socket.socketpair()
        self._parent_sock = parent_sock
        self._child_sock = child_sock
        self._parent = Connection(parent_sock.fileno())
        self.emit = ChildWriter(Connection(child_sock.fileno()))

    def _ensure_process(self):
        if self._process is None:
            self._open_channel()  # 每次拉起换新通道（旧通道已随旧进程 EOF）
            self._process = self.spawn(self.emit)
            self._process.start()
            # spawn 已把 child 端 fd 传进进程；宿主副本必须关闭——否则
            # 子进程死亡（fd 全关）时 parent 端收不到 EOF，reader 不退出。
            # socket 对象同样标记已关（detach）：Connection 与 socket 同持
            # 一个 fd 号，fd 释放后 socket __del__ 不得再关（fd 号可能已被
            # 复用为其它通道，双关即误杀）。宿主侧进程对象的 _conn 同 fd
            # 一并失效，置 None（child 侧副本不受影响）。
            self.emit.conn.close()
            self._child_sock.detach()
            self._process._conn = None
            self._start_reader()
        return self._process

    def _start_reader(self):
        """常驻 reader：child 产出 → 读侧盖章 → event_bus。每次拉起新
        通道（_open_channel）即绑定新 parent 无条件启新 reader——旧通道
        的 reader 已随旧进程 EOF（或通道 close）退出，不判重（判重存在
        terminate 后立即 respawn 的窄窗口漏启风险）。"""
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(self._parent, self._parent_sock),
            name=f"reader-{self.identity}", daemon=True)
        self._reader.start()

    def _read_loop(self, parent, parent_sock):
        try:
            while True:
                try:
                    event = parent.recv()
                except (EOFError, OSError):
                    return  # 子进程死亡（child 端全关）或通道已关闭
                if not isinstance(event, dict):
                    # 子进程产出非事件：响亮丢弃（协议违规，与校验同口径）
                    print(f"[protocol] {self.identity} 产出非 dict 事件，丢弃")
                    continue
                event["source"] = self.identity  # 读侧盖章（进程内不可改写）
                self.event_bus.put(event)
        finally:
            # 确定性关闭 + 标记已关：防 GC 双关 fd 的噪音（fd 号复用误杀）
            parent.close()
            parent_sock.detach()

    def deliver(self, event: Event):
        """投递事件（经 parent 端 Connection，惰性拉起）。"""
        self._ensure_process()
        try:
            self._parent.send(event)
        except OSError as exc:
            # 子进程已死（child 端关闭）：响亮丢弃，不击穿内核循环
            # （与校验失败丢弃同口径；终止契约仍由 terminate 收尸）
            print(f"[protocol] 投递 {event.get('payload', {}).get('command')!r}"
                  f" 到 {self.identity} 失败（进程已死）: {exc}")

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
