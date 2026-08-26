---
kind: task
phase: v0.14
source: SPEC.md
priority: high
---

# 沙箱包裹层（bwrap 固定矩阵）

## 内容

所有设备进程（及 agent）默认进沙箱：bwrap 重执行（spawn 子进程入口，
保留 argv/fd、模块顶层幂等重入）+ userns/pidns + setrlimit（CPU/内存/
进程数）+ 系统路径只读 + 数据区唯一写根 + 默认禁网。
**固定矩阵，不承载权限**——无按 position 的挂载物化；设备进程永远不是
root；跨区访问走调用级裁决（本卡不涉及）。

## 技术要点

- **机制：run() 顶部沙箱重执行 + 自定义 re-entry（非 argv 重执行）**。
  spawn 子进程的 argv = [main_path]、进程上下文来自 spawn_main 一次性
  管道（已实测）——argv 重执行会重跑入口、spawn_main 重跑管道已空，均
  不可行。改为：run() 检测未沙箱 → 把装载状态（inbox/emit/load_spec/
  max_concurrent_sources/data_dir）pickle 到继承 fd → os.execv(bwrap +
  binds + `python -m my_team.kernel.sandbox_entry <fd>`)，沙箱内 re-entry
  反序列化并直接运行 serve 循环（不重跑 spawn_main/入口模块）；
- `--ro-bind / /` + `--proc /proc` + `--tmpfs /tmp` + `--bind <data_dir>`
  + `--unshare-user --unshare-pid --unshare-net --die-with-parent`；
- setrlimit 在 re-entry 首行设置（或 prlimit 包装）；
- 网络开关参数由 network-declaration 卡提供（本卡只做 exec 骨架，
  参数通道编辑边界不相交）。
- 环境前置补实测：spawn 子进程在 bwrap 内 + mp.Queue 通联冒烟。

## 验收

- 设备进程 FS：只读系统、只写自己数据区、不可见他人数据区；
- 沙箱内无法 kill/ptrace 兄弟进程与内核（pidns + Yama）；
- **固定矩阵：不同 position 设备挂载矩阵一致（无 per-position 物化、
  无 root 越权挂载）**；
- 进程内 `os.getpid()` 等行为正常（spawn 语义不破坏）；
- 全量回归不回归。

## 依赖

data-dir-convention（已提交）；被 bash-sandbox-adapt、outbound-channel 依赖。
