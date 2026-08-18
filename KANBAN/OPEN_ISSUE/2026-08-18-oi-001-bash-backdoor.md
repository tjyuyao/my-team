---
kind: issue
status: open
---

# OI-001: 开放 Bash 工具（万能后门）— 禁止直接加入

**Opened:** 2026-08-17 (v0.6.0 review, §五/§八/§九)
**Status:** OPEN — 明确禁止在 ToolManifest + 沙箱协议完成前加入 `bash`

## 问题

Bash 不是普通工具，而是"可启动任意进程、访问文件系统、继承环境、
产生网络和外部副作用的通用执行入口"。直接注册 `tools = ["bash"]`
会绕过当前全部权限体系：

- **绕过工具权限**：即使 Agent 没有 `read`/`write`/`ls`，也可通过
  `cat`、`ls`、`rm` 访问其他 Agent 私人空间、共享 KB、宿主机文件
- **绕过锁与版本**：`git commit`、直接改文件不经 TransactionBuffer /
  SharedKB 单写入口
- **副作用不可推断**：一条命令可能创建/删除/重命名文件、启动后台
  进程、发网络请求、修改环境变量；无法仅凭工具名判断副作用类型
- **破坏可暂停**：`sleep 100` / `make test` 执行中无法瞬间暂停；
  需要 process-group 信号语义（SIGTERM → grace → SIGKILL）
- **破坏回滚**：`python migrate.py` / `curl -X POST` 无法被 kernel
  snapshot 撤销；必须显式声明 `reversible: false` + 补偿策略
- **宿主风险**：`subprocess.run(shell=True)` 可触达 SSH keys、
  Docker socket、系统设备、环境 secrets

## 影响

当前（v0.6.0）若加入 Bash，等于给 Agent 一把绕过权限、Snapshot、
事务、审计全体系的"万能后门"。

## 为什么不现在解决

v0.6.0 的定位是"异步 runtime prototype"——工具协议
（ToolManifest → PreValidate → ToolRequest → IsolatedExecutor →
ToolResult → EffectManifest → CommitValidate）尚未建立，沙箱
（独立 worker / 容器、文件系统挂载、网络 deny-by-default、资源限制）
尚未实现。

## 触发条件（必须解决 / 重新评估的时机）

1. **v0.7.0 目标实现后**：ToolManifest、OperationPolicy、受限工具
   （`apply_patch`、`run_tests`、`git_diff`）先于 Bash 落地
2. 若未来确实需要 Bash，按 v0.6.0 审查 §九 + v0.7.0 审查 §六 的顺序实施：
   - 独立 Bash Worker / 沙箱（禁止宿主机 `shell=True` 直连；
     `shell=False` 也只是降低注入面，不等于隔离）
   - **临时 workspace 先行**：在沙箱 base（当前 workspace 版本）上
     执行 → 产生 diff/effect manifest → CommitValidate → 以
     patch/merge 方式应用到新版本。失败不污染正式工作区；变更可
     审查、可冲突检测、可选择性合并、可完整审计
   - 每次调用获得临时工作目录，仅挂载授权路径
   - 网络默认 deny；仅白名单 host/port/method，且限制请求/响应字节
   - 资源限制必须是**执行器强制**的（wall-clock、CPU、内存、进程数、
     输出字节、fd 数、文件大小、磁盘配额）——manifest 声明不是边界
   - 命令审批策略：ALLOWLISTED / POLICY_CHECKED / HUMAN_APPROVAL /
     DENIED（注意 allowlist 不能替代沙箱：`python -c "..."` 可做任意事）
   - 工具收到的 `agent_id`/`simulation_id`/权限范围/工作目录由系统注入，
     不允许 LLM 或参数自指；git 类命令固定 cwd 与
     GIT_DIR/GIT_WORK_TREE/HOME/GIT_CONFIG_NOSYSTEM，参数仅系统构造
   - Bash 的副作用无法被完整观察：ToolResult 区分
     declared / observed / possible effects，网络副作用记为不可回滚
3. **v0.7.0 审查后追加**：Bash 是 `SandboxedProcessCapability` 而非
   `tools = ["bash"]`——至少涵盖 process_spawn / filesystem_write /
   filesystem_delete / network / environment / secrets / child_process /
   resource_consumption 九个维度

## 推荐的替代路径（不阻塞主线路）

受限工具优先序：`read_file` → `list_files` → `apply_patch` →
`run_tests`（真正隔离后）→ `git_diff` → `git_status` →
`python_compute`（L0）→ `python_transform`（L1）→
`isolated_python`（L2，SANDBOXED_PROCESS）→ `restricted_bash`。
`sandboxed_python` 按 2026-08-17 设计评审拆分为执行等级 L0–L4
（SPEC §8.7「执行等级」+ v0.8.0 计划 P1-7）：L0/L1 是能力削减 +
进程隔离（LOCAL_PROCESS，防意外不防恶意逃逸），L2 才声明
SANDBOXED_PROCESS。`apply_patch` 比通用 `write` 更易审计，
`run_tests` 比通用 Bash 更易限制。v0.7.0 已落地
`apply_patch`/`run_tests`(LOCAL_PROCESS)/`git_diff`/`git_status`；
**v0.8.0 已落地 `python_compute`/`python_transform`（L0/L1，
经 Executor Admission + dispatch 在 tick 流中执行，支持物理取消）**。
