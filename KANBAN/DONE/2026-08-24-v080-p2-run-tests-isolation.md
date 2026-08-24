---
kind: task
status: completed
phase: v0.10（v0.8 P2 遗留收尾；不依赖扩展表面，可并行）
source: SPEC §6.3、§15；KANBAN/PLAN/v0.8.0-plan（P2-7）
priority: medium
---

# v0.10-16a: v0.8 遗留 — run_tests 真实隔离（P2-7）

**Status:** DONE
**Completed:** 2026-08-24

## 目标
`run_tests` 从同进程调用升级为真实隔离的执行等级
（SANDBOXED_PROCESS），收掉 v0.8.0 计划 P2-7。

## 要求 / 规则
- 只读挂载（临时工作区副本）、网络 deny-by-default、资源限制
  （CPU/内存/进程数/文件大小）、环境净化（sitecustomize/PYTHONPATH/
  PATH/secret 剥离）、GIT_* 固定。
- 达成后 `run_tests` 执行类升级为 `SANDBOXED_PROCESS`。

## 产出
- SANDBOXED_PROCESS 执行等级与 run_tests 升级。

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **现状事实**：`ExecutionClass.SANDBOXED_PROCESS` 枚举已存在
  （tool_manifest.py），但 run_tests 实际声明 `LOCAL_PROCESS`；
  `sandbox_tools.run_sandboxed_process` 只有 timeout + 输出截断 + 进程组
  杀，只读挂载 / 网络 deny / 资源限制 / 环境净化均未提供（OI-001 可落地
  部分）。
- **平台相关（按决策 4 处理）**：内核定义 ExecutionClass 规范与约束声明，
  隔离后端可插拔；真实隔离后端仍需实现（mount/rlimit/cgroups 等）。
- **验证难**：测试需证明网络真被拒、环境真净化（非自证）。
- **并入「工具执行环境对齐」**：T17 by-product 的 run_tests/git cwd 宿主
  目录问题并入本卡（同一段代码，分开做必冲突）。

## 实现注记（2026-08-24）
- **内核规范**（新模块 `src/my_team/sandbox_spec.py`）：
  `SandboxConstraints` 声明式约束（CPU/内存/进程数/文件大小 rlimit、
  环境净化 PYTHON*/secret/PATH/GIT_*、deny_network、isolated_mount、
  readonly_binds）+ `SandboxBackend` 可插拔后端 Protocol（决策 4）。
  `ToolManifest.sandbox_constraints` 字段：SANDBOXED_PROCESS 必填、
  其余执行类禁填、且不得声明 requires_network（网络由沙箱 deny）。
- **真实隔离后端**（sandbox_tools.py）：可信 shim 子进程（
  `python -c` → 设 rlimit → userns/mountns/netns unshare → execvpe，
  同 PID，进程组杀/取消语义不变；避免 preexec_fn 线程不安全）。
  最低特权路径：`unshare(CLONE_NEWUSER)`（unprivileged userns，无需
  root）+ uid/gid_map + setuid(0) + 按需 `CLONE_NEWNET`/`CLONE_NEWNS`；
  只读 bind = MS_BIND + MS_REMOUNT|MS_RDONLY（须先
  `mount --make-rprivate /`，且 remount 回放 mountinfo 原始
  source/fstype/options——与 util-linux 同法，探明 EPERM 根因）。
  每约束实际应用与否进 `sandbox_report`（{constraints, applied,
  notes}），deny-by-default 报告，绝不静默降级。
- **run_tests 升级**：manifest v2.0.0 `SANDBOXED_PROCESS` +
  `SandboxConstraints`（cpu 60s / 内存 512MiB RLIMIT_AS / 进程 64 /
  文件 16MiB / PYTHONPATH·PYTHONHOME·PYTHONSTARTUP·PYTHONUSERBASE 剥离 /
  secret 关键词剥离 / PATH=/usr/bin:/bin / GIT_* 固定 + HOME 重定向 /
  deny_network / isolated_mount）；`requires_network=False`（网络由沙箱
  deny，policy 不再拦）；执行器 tier 升 `SANDBOXED_OUT_OF_PROCESS`，
  dispatch 分支同跑内核 handler（工具在沙箱 OS 进程执行）。
- **cwd 宿主目录问题（T17 by-product）**：`make_workspace_copy()` 快照
  复制工作区（排除 .git/.venv/.uv-cache/缓存/tmp/private/.claude）为
  cwd；相对 test_path 解析进副本；pytest 的 .pytest_cache/__pycache__/
  tmp 写入只落在副本、随 TemporaryDirectory 消亡，宿主零污染
  （测试用"子进程写相对文件→宿主不出现"实证）。git_diff/git_status 仍
  需真实 .git 留在宿主 cwd，仅补 `pinned_git_env`（GIT_CONFIG_NOSYSTEM/
  GIT_TERMINAL_PROMPT 固定、GIT_DIR/GIT_WORK_TREE 等 unset）。
- **测试（非自证，`tests/test_sandbox_isolation.py` 16 项）**：子进程
  真实操作断言失败——内存超限 MemoryError、文件超限 EFBIG、fork 超限
  EAGAIN、CPU 超限被杀（各有无限制对照组）；网络 deny 后连 127.0.0.1
  都不可达且 `if_nameindex()==['lo']`（对照组宿主 loopback 通）；
  只读 bind 写返回 EROFS（对照组可写）；sitecustomize 用 marker 实证不
  生效（对照组生效）；tool 级集成证临时副本 + 环境净化 + rlimit 读回 +
  sandbox_report。
- **环境限制**：本机（Linux, unprivileged userns 可用）netns 与只读
  mount 全绿，无 skip；无特权主机（userns 被禁）对应约束 applied=false
  并注明，测试条件 skip——后端代码路径真实存在，报告不静默。
- **并行子代理**：T16b（token budget）并行改 simulation.py 同文件
  不同区域；提交以 hunk 级 staging 隔离，未带他人文件；全量 pytest 期间
  其泄漏的 ControlPlane 端口曾短暂占用 18101-18106（其会话结束后释放，
  与本次改动无关）。

## 验收标准
- [x] run_tests 在只读挂载 + 网络拒绝 + 资源限制下执行
- [x] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
