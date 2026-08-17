# v0.8.0 Implementation Plan — Durable, Fenced, Isolated Tool Operations

**Created:** 2026-08-17（v0.7.0 review 后）
**Status:** TODO
**2026-08-17 设计评审：** sandboxed_python 分层定稿（L0–L4 → P1-7 与
SPEC §8.7「执行等级」）
**Label:** v0.8.0 — Durable, fenced, and isolated tool operations

## 定位

v0.7.0 建立了 Manifest-based policy-controlled tool runtime prototype
（工具契约、策略、两阶段 Validate、受限本地工具、基本取消）。
v0.8.0 把工具运行时从"原型"推向"可持久、可隔离、可恢复"：

```text
Frozen Snapshot
+ ToolRequest（带 manifest_hash / input_hash / state_epoch）
+ Executor Admission
+ Effect Group（atomicity）
+ CommitValidate
+ Outbox / Compensation（持久化）
+ State Epoch
```

**显式不做：** 开放通用 Bash（OI-001：Worker + 临时 workspace +
diff→merge + 网络拒绝 + 执行器强制资源限制完成前禁止）。

## P1: Must Complete

### 1. Pending Operation 持久化

- PendingOperationRegistry 全量入 SimulationStore（v0.6.0 P3-11 已存
  ops；补齐**跨重启继续执行**语义：SUBMITTED/PENDING ops 在 load 后
  可被外部执行器完成、结果正常 ingest）
- 测试：save 时 in-flight op → 重启 → 外部完成 → 结果投递（含
  state_epoch fencing 跨重启）

### 2. Outbox 持久化

- OutboxEntry（committed 未 dispatch / dispatch 中 / 重试中）持久化；
  重启后继续 dispatch（幂等 key 防重）
- 测试：dispatch 中断 → load → 续投；idempotency key 跨重启去重

### 3. ToolRequest / ToolResult 契约完整化

- ToolRequest：request_id / agent_id / task_id / tool_name /
  tool_version / manifest_hash / input_hash / state_epoch /
  workspace_version / deadline_tick（系统注入，插件不可自指）
- ToolResult：request_id / status / exit_code / stdout / stderr /
  output_hash / effects / possible_side_effects /
  executor_cancel_confirmed；区分 declared / observed / possible
  effects
- 工具版本与 manifest_hash 进入审计与回放上下文

### 4. Executor Admission

- 工具协议插入 Admission 阶段：worker 可用性、容量、配额分配、
  审批流（requires_approval → HUMAN_APPROVAL 决策路径）
- 远程工具执行器从"外部 harness"（FakeToolExecutor 模式）升级为
  注册的执行器（executor_kind / trusted_level 分级）

### 5. 工具执行器分级

- TRUSTED_IN_PROCESS（内置：read/ls/write/kb_write/send_email/
  delegate/apply_patch）
- UNTRUSTED_OUT_OF_PROCESS（第三方插件，独立进程）
- SANDBOXED_OUT_OF_PROCESS（run_tests 真正隔离后；isolated_python L2）
- 插件注册安全模型：register_manifest 与 register_executor 分离；
  注册不得改全局状态/拿他 agent 权限/同名覆盖/绕过策略；身份字段
  一律系统注入

### 6. 工具请求幂等

- request_id 全局去重（跨重启，persist 已见 key）
- 同一 ToolRequest 重放不重复计费/不重复副作用

### 7. sandboxed_python 执行等级（L0/L1）

2026-08-17 设计评审定稿（SPEC §8.7「执行等级」）：`sandboxed_python`
是受策略约束的 Python 执行服务，不是单一工具。

- L0 `python_compute` — LOCAL_PROCESS；无文件/网络/子进程；受限
  标准库白名单 + 受限 builtins；JSON 输入 → 结构化 result schema
  验证；复用 sandbox_tools（超时/截断/进程组终止）
- L1 `python_transform` — LOCAL_PROCESS + 临时工作区协议：只读输入
  副本、独立 output 目录、artifact manifest；无网络/无 secrets；
  artifact 提交 = 带 base_hash 的 STAGED_MUTATION（复用 FILE_PATCH
  apply-time 复查；版本不符 → 局部 workspace_conflict）
- L2 `isolated_python`（SANDBOXED_PROCESS）— 依赖 P2-7 真隔离的
  沙箱基础设施，与 run_tests 同门禁；L3/L4 不实现

**Acceptance:**
- 代码在独立子进程执行（绝不在主进程 exec）；`-I` 隔离模式 +
  剥离 PYTHONPATH/sitecustomize/环境变量；受限 builtins + import
  gate 与 manifest 的 allowed_modules 一致
- 取消 = 进程组物理终止（executor_cancel_requested/confirmed=True，
  P2-10 的首个达成者）；无法确认 → CANCEL_UNCONFIRMED
- L0/L1 定位如实声明：防意外，非防恶意逃逸（文档注明）
- 模式 A（code + inputs）先行；模式 B（entrypoint + 文件）延后

## P2: Should Complete

### 7. run_tests 真实隔离（SANDBOXED_PROCESS 才有资格）

- 只读挂载（临时工作区副本）、网络 deny-by-default、资源限制
  （CPU/内存/进程数/文件大小）、环境净化（sitecustomize/PYTHONPATH/
  PATH/secret 剥离）、GIT_* 固定
- 达成后 run_tests 由 LOCAL_PROCESS 升为 SANDBOXED_PROCESS

### 8. Snapshot / rollback 集成测试矩阵

- Snapshot Coverage Matrix（TaskTree / Scheduler claims / Pending ops /
  Private files 版本视图 / Shared KB / 外部进程 / LLM 请求 / ID 分配 /
  state_epoch）逐行验证 Freeze 可见性 / Commit 可回滚性 / 持久化

### 9. 跨进程恢复测试

- worker 崩溃 → op FAILED/TIMED_OUT → agent 结构化唤醒 → retry
- 模拟进程重启多次，审计与状态收敛

### 10. 取消语义完整化

- LOCAL_PROCESS 工具执行中取消 → 实际终止进程组
  （executor_cancel_requested=True → confirmed=True）
- CancellationResult 进入审计

### 11. token/cost 预算（v0.7.0 P2-7 遗留）

- 定价表 + 每 agent/task/simulation 上限，PreValidate 拒绝；
  concurrency / request_count / token / cost / wall_time 分列

## 迁移顺序（建议）

```
 1. Outbox + pending ops 持久化闭环（P1-1/2）
 2. ToolRequest/ToolResult 契约 + manifest_hash 审计（P1-3）
 3. Executor Admission + 执行器注册（P1-4/5）
 4. 请求幂等（P1-6）
 5. sandboxed_python L0/L1（P1-7；与 1-4 无依赖，可并行）
 6. run_tests 真实隔离（P2-7，L2 isolated_python 的沙箱前置）
 7. Snapshot 矩阵 + 跨进程恢复测试（P2-8/9）
 8. 取消物理化 + token/cost 预算（P2-10/11）
```

## 明确的非目标

- ❌ 开放通用 Bash（OI-001）
- ❌ 多实例并发写同一 DB（SQLite 单写者；多实例为 v0.9+）
- ❌ 内核级插件阶段钩子（业务逻辑活在工具 + runtime 层，kernel 固定
  10 阶段不变）

## 验证

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src/my_team
uv run pytest --cov=my_team --cov-branch --cov-report=term
```
