---
kind: task
phase: v0.14
source: SPEC.md
priority: high
---

# 沙箱包裹层（bwrap 固定矩阵 + 传输层重写）

## 内容

所有设备进程（及 agent）默认进沙箱：bwrap 重执行（spawn 子进程入口，
保留 fd、模块顶层幂等重入）+ userns/pidns/netns/ipcns + setrlimit（CPU/
内存/进程数）+ 系统路径只读 + 数据区唯一写根 + 默认禁网。
**固定矩阵，不承载权限**——无按 position 的挂载物化；设备进程永远不是
root；跨区访问走调用级裁决（本卡不涉及）。

**方案 B 定案（已实现 b83bda6）**：所有进程同 Linux uid 运行，Linux 的
uid 隔离用不上；Agent/设备互相隔离是系统语义承诺，沙箱必须物理实现——
**沙箱内除宿主显式传入的继承 fd 外不得存在任何跨进程通道**。mp.Queue
依赖 /dev/shm 命名信号量（同 uid 可枚举/打开/干扰/填满共享区），与隔离
承诺冲突 → 传输层重写为 fd 继承的 socketpair + `multiprocessing.connection.
Connection`；宿主读侧盖章（原 outbound-channel 卡内容）随重写一并落地。

## 技术要点

- **机制：run() 顶部沙箱重执行 + 自定义 re-entry（非 argv 重执行）**。
  spawn 子进程的 argv = [main_path]、进程上下文来自 spawn_main 一次性
  管道——argv 重执行会重跑入口、spawn_main 重跑管道已空，均不可行。
  改为：run() 检测未沙箱（哨兵环境变量）→ 把装载状态（连接 fd/
  load_spec/max_concurrent_sources/data_dir/agent 或设备标志）pickle 到
  继承 fd（os.pipe，状态须远小于 64KB 缓冲）→ os.execv(bwrap + binds +
  `python -m my_team.kernel.sandbox_entry <fd>`)，沙箱内 re-entry
  反序列化并直接运行 serve 循环（不重跑 spawn_main/入口模块）；
- **传输层（方案 B）**：每子进程一条 socketpair，宿主持 parent 端
  Connection（deliver=send；常驻 daemon reader 线程 recv → 按进程归属
  盖章 `source=identity` → event_bus），子进程持 child 端（读事件 /
  emit 写入器 send，事件不含 source，无可改写身份字段）。宿主直投
  event_bus 保留。UserModeProcess 去 mp.Queue inbox；
- **沙箱判定（probe 豁免）**：load_spec 非空（设备）或有 workdir 属性
  （agent）→ 沙箱；裸 UserModeProcess（测试探针）→ 不沙箱；
- 固定矩阵命令行：`--ro-bind / / --proc /proc --tmpfs /tmp
  --bind <data_dir> --unshare-user --unshare-pid --unshare-net
  --unshare-ipc --die-with-parent`（--unshare-ipc 封 System V IPC）；
- setrlimit 在 re-entry 首行设置（CPU 60s / AS 1GB / NPROC 64，实测保守）；
- 网络开关参数由 network-declaration 卡提供（本卡只做 exec 骨架 +
  默认禁网；`_bwrap_args(data_dir, state_fd, needs_network=False)` 单
  函数预留参数通道，编辑边界不相交）。

## 验收

- 隔离面完整：FS（挂载矩阵）/ 信号（pidns）/ ptrace（Yama）/ 网络 +
  abstract socket（netns）/ System V IPC（ipcns）——沙箱内除继承 fd 外
  无跨进程通道；
- 设备进程 FS：只读系统、只写自己数据区、不可写他人数据区（--ro-bind
  / / 下他人数据区只读可见；严格不可见需掩蔽 data 父目录、对 agent 不
  可行——按弱侧"不可写"验收）；
- 固定矩阵：不同 position 设备挂载矩阵一致（无 per-position 物化、
  无 root 越权挂载）；
- 进程内 os.getpid() 等行为正常（spawn 语义不破坏）；
- 出站：子进程内无可改写身份字段；宿主 reader 盖章；宿主直投保留；
- terminate 契约（超时强杀 SIGKILL bwrap → --die-with-parent 连坐）；
- 全量回归不回归。

## 依赖

data-dir-convention（已提交）；被 bash-sandbox-adapt 依赖；outbound-channel
内容已并入本卡（读侧盖章）。
