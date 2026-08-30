"""Sandbox re-entry：bwrap 沙箱内入口（``python -m my_team.kernel.sandbox_entry <fd>``）。

首行 setrlimit（保守值，沙箱内实测可设）→ 从继承 fd 反序列化装载状态 →
以 fd 整数重建 child 端 Connection → 按 kind serve：
- device：按 load_spec 加载模块构造 Device 实例（复用原 run 装载方式）；
- agent：状态里是完整可 pickle 的 Agent 实例（连接已剥离），补回连接后
  直接 serve。

不重跑 spawn_main、不重跑入口模块顶层。fd 继承：socket fd 在 execv 前
set_inheritable，编号保留；状态里存 fd 整数，此处 Connection(fd) 重建
（不直接 pickle Connection，避开 reduction 的 DupFd 语义）。
"""

import asyncio
import os
import pickle
import resource
import sys
from multiprocessing.connection import Connection

from my_team.kernel.process_handle import ChildWriter

# setrlimit 保守取值（沙箱内实测可设）：
# - RLIMIT_CPU 60s：进程 CPU 时间上限（bash 设备 job 子进程计入）；
# - RLIMIT_AS 1GB：地址空间上限（演示级设备内存有界）；
# - RLIMIT_NPROC 64：进程数上限——userns 内按命名空间进程计数（bash
#   job 池/子进程都在其内），取值保守。
RLIMITS = [
    (resource.RLIMIT_CPU, (60, 60)),
    (resource.RLIMIT_AS, (1 << 30, 1 << 30)),
    (resource.RLIMIT_NPROC, (64, 64)),
]


def _setrlimit():
    for limit, value in RLIMITS:
        resource.setrlimit(limit, value)


def _serve_device(load_spec, conn):
    """按装载描述在沙箱内装载设备（复用原 run 的装载方式）并 serve。"""
    import importlib.util
    import inspect
    import uuid

    from my_team.kernel.process import UserModeProcess

    identity, path, options = load_spec
    name = f"team_device_{identity}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    # needs_network 是沙箱控制字段（process._needs_network 读取），非设备
    # 构造参数——过滤后 Device 才收自己的 options（不含沙箱开关）。
    inst_opts = {k: v for k, v in options.items() if k != "needs_network"}
    # 注入 runtime_root 和 identity（ProcessBase 必需）
    runtime_root = os.environ.get("MY_TEAM_DATA_DIR", "/tmp")
    inst_opts["runtime_root"] = os.path.dirname(runtime_root)  # home 的上级
    inst_opts["identity"] = identity
    # 查找 Device 类：优先 Device，否则找第一个 UserModeProcess 子类
    device_cls = getattr(module, "Device", None)
    if device_cls is None:
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type) and issubclass(attr, UserModeProcess)
                    and attr is not UserModeProcess):
                device_cls = attr
                break
    if device_cls is None:
        raise AttributeError(
            f"设备源码无 Device 类或 UserModeProcess 子类: {path!r}")
    # 自动填充缺失的默认值（兼容旧设备签名）
    sig = inspect.signature(device_cls.__init__)
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "emit", "runtime_root", "identity"):
            continue
        if param_name not in inst_opts and param.default is inspect.Parameter.empty:
            # 提供保守默认值
            defaults = {
                "max_concurrent_sources": 0,
                "max_jobs": 8,
                "timeout": 30.0,
                "deadline": 60.0,
                "max_deadline": 300.0,
                "output_cap": 65536,
                "completed_cap": 64,
            }
            if param_name in defaults:
                inst_opts[param_name] = defaults[param_name]
    device = device_cls(ChildWriter(conn), **inst_opts)
    asyncio.run(device._serve())


def _serve_agent(agent, conn):
    """补回连接后直接 serve（不重跑入口模块顶层）。"""
    agent.emit = ChildWriter(conn)
    agent._conn = conn
    asyncio.run(agent._serve())


def main():
    _setrlimit()
    fd = int(sys.argv[1])
    with os.fdopen(fd, "rb", closefd=True) as f:
        state = pickle.load(f)
    conn = Connection(state["conn_fd"])
    if state["kind"] == "device":
        _serve_device(state["load_spec"], conn)
    else:
        _serve_agent(state["instance"], conn)


if __name__ == "__main__":
    main()
