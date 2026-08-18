---
kind: task
status: completed
---

# v0.7.0 Implementation Plan — Snapshot-Consistent, Policy-Controlled Tool Runtime

**Created:** 2026-08-17
**Status:** P1 DONE + review round (f77846f..5908324) — P2 dispositioned, open for v0.8+
**Label:** v0.7.0 — Manifest-based policy-controlled tool runtime **prototype**
**Milestone:** KANBAN/MILESTONE/2026-08-17-v0.7.0.md

## 审查轮次修订（be4765e）

- FILE_PATCH 携带 base_hash/patch_hash/new_content_hash；应用时刻
  复查（同 tick 写 → 局部 patch_conflict）
- Effect 分组原子性（group_id + atomicity；delegate 组）
- 结构化 error_code（ActionResult + 审计 details）
- ExecutionClass.LOCAL_PROCESS；run_tests 重分类 + possible_side_
  effects + requires_network=True
- scheduler claim 回滚 requeue
- cancel_operation → CancellationResult（逻辑取消语义 + LLM 措辞修正）
- v0.8.0 计划建立（KANBAN/TODO/2026-08-17-v080-implementation-plan.md）
- **3db1301（设计评审，v0.8 规划期）**：sandboxed_python 拆分为执行
  等级 L0–L4（SPEC §8.7「执行等级」）——L0/L1 → v0.8.0 P1-7；
  本条 P1-3 第 7 项（sandboxed_python）被取代

## Goal

v0.6.0 完成了核心异步 runtime（Intent → PendingOp → Wake →
Continuation Resume），并硬化了快照读、epoch fencing、超时唤醒与
SQLite 持久化。v0.7.0 的目标是**把"可执行的确定性原型"变成
"可安全执行受限工具的策略受控运行时"**：

```text
ToolManifest
→ Intent
→ PreValidate(Intent + Policy)
→ ToolRequest
→ IsolatedExecutor
→ ToolResult + EffectManifest
→ CommitValidate
→ Commit / Outbox / Compensation
```

**显式不做：** 开放 Bash（见 KANBAN/OPEN_ISSUE/OI-001.md）。

## P1: Must Complete

### 1. Tool Manifest + OperationPolicy — ✅ DONE (f77846f)

**Acceptance:**
- `ToolManifest`（frozen dataclass）：name、version、input/output
  schema、capabilities、effect_types、execution_class
  （PURE / READ_ONLY / LOCAL_DETERMINISTIC / STAGED_MUTATION /
  SANDBOXED_PROCESS / EXTERNAL_IRREVERSIBLE）、deterministic、
  idempotent、reversible、requires_network、filesystem_scopes、
  max_runtime_ms、max_output_bytes、supports_cancel、
  requires_approval、retry_policy
- 现有工具（read/write/ls/kb_write/send_email/delegate）补齐
  manifest；注册即校验
- `OperationPolicy`：allowed / requires_approval / max_wall_time_ms /
  max_output_bytes / network_access / filesystem_scope / retry_policy /
  reversible
- 测试：manifest 必填、capability 作用域、输出上限、超时、取消、
  网络策略、文件系统作用域、需人工审批、effect 审计、版本记录
  （v0.6.0 审查 §十 测试清单）

### 2. Two-Phase Validate 强化 — ✅ DONE (27835fc)

**Acceptance:**
- PreValidate(Intent) 增加：deadline、budget、idempotency、
  operation policy、tool manifest、task 有效性
- CommitValidate(Effect/PendingOp) 增加：lock token 仍有效、
  KB version 仍匹配、task 未取消、deadline 未过、op 未重复提交、
  effect 属于当前 epoch、配额仍够、outbox key 不重复
- 核心原则固化为注释/SPEC：PreValidate 检查"是否允许尝试"；
  CommitValidate 检查"现在是否仍可提交"

### 3. 受限工具（先于 Bash）— ✅ DONE (24ff837)

按审查 §十三 顺序加入：

```text
1. read_file      — READ_ONLY，快照视图读取（v0.6.0 已有 read，改名/别名）
2. list_files     — READ_ONLY，快照视图列出
3. apply_patch    — STAGED_MUTATION，patch 格式校验 + 冲突检测 + 回滚
4. run_tests      — SANDBOXED_PROCESS，只读挂载 + 输出截断 + 超时
5. git_diff       — READ_ONLY，沙箱工作区
6. git_status     — READ_ONLY
7. sandboxed_python — SANDBOXED_PROCESS（可选，P2）
8. restricted_bash   — 不实现（OI-001）
```

**Acceptance:** 每个工具带 manifest + policy；工具通过
`ToolRequest → IsolatedExecutor → ToolResult + EffectManifest` 路径；
`apply_patch` 与 `run_tests` 的副作用可审计、可回滚（或声明
irreversible 并走补偿）。

### 4. 工具超时与取消 — ✅ DONE (18f6964)

**Acceptance:**
- 超时：工具进程被终止（process group），op 标记 TIMED_OUT，
  agent 以结构化错误唤醒（v0.6.0 已有链路的工具版本）
- 取消：`supports_cancel` 的工具可被取消；cancelled op 的结果
  不发布（v0.6.0 已有 registry 语义）
- 超时/取消审计记录

## P2: Should Complete — 处置：延后 v0.8+（评估见 MILESTONE §5）

### 5. Typed AgentSnapshot Views

- `AgentSnapshot` 从 dict 改为类型化视图：EmailView、TaskView、
  KBResourceView、LockView、FileView
- 读一致性与 v0.6.0 冻结视图合并（一份只读视图，两种语义）
- **处置：延后（v0.8+）** — 高破坏性重构（~15 个测试文件消费 dict
  形态），冻结视图语义已由 v0.6.0 实现，原型阶段收益低于成本

### 6. BoundedMicroLoop 重新观察

- 第二轮 micro-loop 使用重新冻结的快照（stale snapshot 修复）
- **处置：N/A（现架构不存在）** — tick 内核不接线 micro-loop
  executor（simulation.py 中 execution_mode 零引用）；意图内核每
  tick 的 Act 都基于冻结快照，无 stale-snapshot 窗口

### 7. Provider 429/5xx 重试 + token/cost 预算

- provider 级重试（退避）与 v0.6.0 agent 驱动重试并存
- 每 agent / task / simulation 的 token/cost 上限，超限在
  PreValidate 拒绝
- **处置：部分延后** — 模拟架构中 provider 是外部 harness（异步
  完成 op），同步重试不适用；重试属 provider 实现层（同 OI-002）。
  token/cost 预算需定价表（FakeLLM 无计价），留 v0.8

### 8. 内容寻址 / 版本化文件历史

- workspace 版本化：`write` → 版本 n；读视图为冻结版本
- 二进制文件纳入快照（v0.6.0 明确排除）
- **处置：延后（可选）** — 与 P2-5 同属视图/历史重构

## 迁移顺序（建议）

```
 1. ToolManifest + OperationPolicy（模型 + 注册校验）
 2. 现有工具补齐 manifest（read/ls → READ_ONLY；write → STAGED_MUTATION）
 3. Two-Phase Validate 强化（deadline/budget/manifest/task 校验）
 4. apply_patch（patch 校验 + 冲突 + 回滚）
 5. run_tests（sandbox：只读挂载 + 超时 + 输出截断）
 6. git_diff / git_status（只读沙箱工作区）
 7. 工具超时/取消接线（process group）
 8. Typed AgentSnapshot + BoundedMicroLoop 重新观察
 9. provider 重试 + token/cost 预算
10. 版本化文件历史（可选）
```

## 明确的非目标

- ❌ 开放 Bash（OI-001：Manifest + 沙箱 + 审批协议完成前禁止）
- ❌ 宿主机直连 `subprocess.run(shell=True)` 类执行
- ❌ 多实例并发写同一 DB（SQLite 单写者；多实例是 v0.8+）
- ❌ 插件打包/注册/信任/生命周期（install/update/signature）→ v0.8+；
  ToolManifest 即插件契约单元（每个工具一个 frozen manifest +
  注册即校验 + effect 声明；插件层只是"一组 manifest + 执行器"的聚合，
  不改 tick 内核。角色逻辑经 custom runtime 挂载，业务状态经
  KB/邮件/任务抽象表达）

## 验证

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src/my_team
uv run pytest --cov=my_team --cov-branch --cov-report=term
```
