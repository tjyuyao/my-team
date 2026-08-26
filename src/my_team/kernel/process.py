"""Kernel：Process 抽象（协议）与两态实现。

Process = 一套契约：``async respond(event) -> Event | VOID``。
- 返回事件 → emit（产出，source 由宿主读侧盖章）；
- 返回 ``VOID``（"VOID" 哨兵）→ 合法沉默，不上总线；
- 返回 None → 协议违规，响亮丢弃（None 无法区分"故意沉默"与"忘记返回"）。

两态同构（接口完全兼容，传输是唯一差异）：
- 用户态 ``UserModeProcess``：真实子进程（mp.Process），事件经 socketpair
  的 Connection（fd 继承，不落 /dev/shm）——收发统一走 emit 携带的
  child 端连接（子进程读事件 = conn.recv、产出 = emit(event)）；宿主
  读侧盖章，进程内不存在可改写身份字段。
- 内核态 ``KernelModeDevice``：与 kernel 同进程托管（可信系统服务，
  well-known 身份，respond 抛错 = kernel 失败）。

沙箱（固定矩阵，不承载权限）：设备（load_spec 非空）与 agent（实例有
workdir 属性）默认进沙箱——run() 检测未沙箱（MY_TEAM_SANDBOXED 哨兵）→
装载状态 pickle 到继承 fd → execv bwrap（只读系统/挂载矩阵双锚点：家 +
源码区，按身份类型展开可写性/默认禁网/ipc 隔离）→ 沙箱内 sandbox_entry
re-entry 直接 serve（不重跑 spawn_main、不重跑入口模块顶层）。

共用 ``BucketDispatcher``：按 source 分桶——同源串行保序、跨源并行。
"""

import asyncio
import multiprocessing as mp
import os
import pickle
import shutil
import sys
from typing import Protocol

from .event_protocol import VOID, Event

# 沙箱哨兵：run() 检测未沙箱（环境变量未设）才 execv bwrap；已沙箱进程
# 若重入 run()（re-entry 应走 sandbox_entry）则响亮失败，防重复沙箱。
SANDBOX_SENTINEL = "MY_TEAM_SANDBOXED"


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
    """用户态进程：真实子进程，事件经 socketpair Connection，同源串行/跨源并行。

    终止 = system 层 payload.command=="terminate" 的事件。
    动态装载设备（load_spec 非空）：Device 实例在子进程内构造——传输的
    只是可 pickle 的装载描述（identity/path/options），不是类对象，故
    spawn/fork 皆可；装载失败 = 进程崩溃（stderr 响亮），属进程级
    故障而非内核裁决。
    """

    def __init__(self, emit, max_concurrent_sources, *, load_spec=None):
        super().__init__()
        self.emit = emit  # 产出通道（可调用；子进程内无身份字段）
        # child 端 Connection（收事件）：宿主 socketpair 的进程侧。
        # emit 可调用即可（如直接实例化时传入收集器），conn 可缺省。
        self._conn = getattr(emit, "conn", None)
        self.max_concurrent_sources = max_concurrent_sources
        # (identity, module_path, options, bound_agent) | None；
        # bound_agent 仅 per-agent 实例非 None（挂载锚点按绑定 agent 解析）
        self._load_spec = load_spec

    # 沙箱网络声明（进程级资源开关）：设备经 load_spec options 声明，
    # agent 经构造参数覆盖；基类默认禁网，未声明即 False。
    needs_network = False

    async def respond(self, event: Event) -> Event | VOID:
        """处理单个事件，返回产出事件或 VOID（子类实现）。"""
        raise NotImplementedError

    def run(self):
        """子进程主循环：设备（load_spec）/agent（workdir）默认进沙箱——
        顶部检测未沙箱（哨兵）→ 装载状态 pickle 到继承 fd → execv bwrap
        固定矩阵，由 sandbox_entry 在沙箱内 serve；裸进程（探针，两者
        皆无）直接 serve。"""
        if self._load_spec is not None or hasattr(self, "workdir"):
            if os.environ.get(SANDBOX_SENTINEL) != "1":
                self._sandbox_reexec()
            raise SystemExit(
                "已沙箱进程不得重入 run()（re-entry 走 sandbox_entry）")
        asyncio.run(self._serve())

    def _sandbox_reexec(self):
        """顶部沙箱重执行：装载状态 pickle 到继承 fd → execv bwrap（固定
        矩阵）→ 沙箱内 sandbox_entry re-entry 从 fd 读状态直接 serve。

        状态只含 int/str/tuple/load_spec/可 pickle 实例，远小于管道缓冲，
        写端不阻塞。fd 继承：child 端 socket fd 经 spawn 传入（默认
        CLOEXEC，execv 会关）——execv 前 set_inheritable；状态里存 fd
        整数，re-entry 用 Connection(fd) 重建（不直接 pickle Connection，
        避开 reduction 的 DupFd 语义）。
        """
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise SystemExit("沙箱需要 bwrap（PATH 中未找到可执行文件）")
        conn_fd = self._conn.fileno()
        os.set_inheritable(conn_fd, True)  # execv 后 fd 编号保留
        state = self._sandbox_state()
        state["conn_fd"] = conn_fd
        writable, readonly = self._mount_anchors()
        for anchor in (*writable, *readonly):
            os.makedirs(anchor, exist_ok=True)  # bwrap --bind 源必须存在
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        with os.fdopen(write_fd, "wb", closefd=True) as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        env = dict(os.environ)
        env[SANDBOX_SENTINEL] = "1"
        env["PYTHONPATH"] = os.pathsep.join(sys.path)  # re-entry 可 import
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # 只读系统下不写 __pycache__
        os.execvpe(bwrap, _bwrap_args(writable, readonly, read_fd,
                                       self._needs_network()), env)
        raise SystemExit("execvpe 失败")  # 理论不可达（exec 成功即替换映像）

    def _sandbox_state(self) -> dict:
        """装载状态（pickle 到继承 fd）：设备传 load_spec；agent 传实例的
        浅拷贝——剥离连接与进程机制（spawn 上下文外的 Connection/authkey
        不可 pickle）。注意剥离的是拷贝：原实例的 Connection 必须活到
        execv（其 __del__ 会关 fd）。"""
        if self._load_spec is not None:
            return {"kind": "device", "load_spec": self._load_spec}
        import copy
        instance = copy.copy(self)
        instance.emit = None
        instance._conn = None
        instance._config = {}  # 去 authkey（非 spawn 上下文 pickle 即报错）
        instance._popen = None
        instance._sentinel = None
        return {"kind": "agent", "instance": instance}

    def _mount_anchors(self) -> tuple[list[str], list[str]]:
        """挂载矩阵锚点（(可写列表, 只读列表)），按身份类型展开为家 + 源码
        区两个锚点（静态出生定格，无 per-position 物化）：

        - 设备（load_spec）：家可写（shared = data/<device-id>；per-agent =
          绑定 agent 的家 data/<bound-agent>，命令落 agent 家），源码区
          data/devices 只读（加载实现用）；
        - agent（workdir 属性）：家 data/<agent-id> 可写 + 源码区可写（生产
          源码；装载权在 Authority，写了也装不了）。

        源码区是系统唯一识别区（bootstrap 只扫这里）；workdir 根仅 data/。
        """
        if self._load_spec is not None:
            identity, path, _, bound_agent = self._load_spec
            workdir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
            if bound_agent is not None:
                home = os.path.join(workdir, "data", bound_agent)
            else:
                home = os.path.join(workdir, "data", identity)
            return [home], [os.path.join(workdir, "data", "devices")]
        return [os.path.join(self.workdir, "data", self.identity),
                os.path.join(self.workdir, "data", "devices")], []

    def _needs_network(self) -> bool:
        """声明通道（默认禁网，显式声明才放行，进程级资源开关非权限
        scope）：设备经 load_spec 的 options.needs_network（安装 payload
        options 携带），agent 经构造参数 needs_network。"""
        if self._load_spec is not None:
            return bool(self._load_spec[2].get("needs_network", False))
        return self.needs_network

    async def _serve(self):
        dispatcher = BucketDispatcher(self.respond, self._emit,
                                      self.max_concurrent_sources)
        while True:
            event = await asyncio.to_thread(self._conn.recv)
            if event.get("kind") == "system" and \
                    event.get("payload", {}).get("command") == "terminate":
                break
            dispatcher.submit(event)

    async def _emit(self, event: Event):
        """产出事件：写 child 端 Connection（source 由宿主 reader 盖章）。"""
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


def _bwrap_args(writable: list[str], readonly: list[str], state_fd: int,
                needs_network: bool) -> list[str]:
    """bwrap 固定矩阵命令行（唯一构造点；网络开关编辑边界——needs_network
    声明通道见 ``_needs_network``，默认禁网，仅显式声明进程保留网络面）。

    固定矩阵语义：挂载参数只依赖身份类型的两个锚点（家 + 源码区），不依赖
    position（无 per-position 物化）；设备进程永远不是 root（userns 单用户
    映射）。data 根 tmpfs 掩蔽其它家的目录可见性——沙箱内除自己的家与
    源码区外，数据根下无其它路径（设备数据物理不可见，只经接口暴露）。
    """
    data_root = os.path.dirname(writable[0])  # 家 = <data_root>/<id>
    args = [
        "bwrap",
        "--ro-bind", "/", "/",          # 系统只读
        "--proc", "/proc",              # 独立 /proc（pidns 内视图）
        "--tmpfs", "/tmp",              # 可写临时区
        "--tmpfs", data_root,           # 掩蔽数据根下其它家（不可见）
    ]
    for anchor in readonly:
        args += ["--ro-bind", anchor, anchor]  # 源码区只读（设备加载实现用）
    for anchor in writable:
        args += ["--bind", anchor, anchor]     # 家（可写锚点）
    args += [
        "--unshare-user",               # userns：设备进程永远不是 root
        "--unshare-pid",                # pidns：不可见宿主/兄弟进程
        "--unshare-ipc",                # ipcns：封 System V IPC 通道
    ]
    if not needs_network:
        args.append("--unshare-net")    # netns：默认禁网（abstract socket 亦隔离）
    args += [
        "--die-with-parent",            # 宿主杀 bwrap 父 → 沙箱连坐
        sys.executable, "-m", "my_team.kernel.sandbox_entry", str(state_fd),
    ]
    return args
