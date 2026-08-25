---
kind: task
status: completed
phase: v0.11 扩展表面
source: 审阅 P0-5、§三；OPEN_ISSUE Authority 模型
priority: high
---

# Authority 裁决算法：DecisionClaim + context 匹配 + composition + escalation

> **状态（2026-08-24 三态收敛）**：核心已实现（`authority.py`，
> 32 测试，零内核依赖）；**剩余接点（与 simulation.py
> Validate/Commit 集成、DecisionClaim 挂 task_id）已并入
> N5 任务治理绑定**。本卡保留为历史与设计依据。


## 目标
把 Authority 从"配置文件"变成"可执行裁决算法"。当前代码只有
`ToolManifest` + `OperationPolicy`（权限 deny-by-default，属闭包授权），
**无 Authority**。Authority 裁决的是 decision claim（冲突时谁终局），
不是权限。

## 要求 / 规则
- `DecisionClaim` 内核对象：`claim_id, subject, effect_ref,
  domains(list[DomainClaim]), context, requested_by(Principal),
  process_instance_id, authority_snapshot`。
- claim 产生映射：定义哪些 Intent/effect 产生 claim；一个 Intent 可涉多
  domain；一个 effect 须同时满足其覆盖的所有 domain；**明确 claim 在
  Validate 还是 Commit 解决**（建议：裁决在 Validate 得出 binding，
  Commit 只做动态重检）。
- context 匹配算法（先于实现固定）：
  1. 按 context specificity 从高到低匹配；
  2. 同 specificity 下显式冲突不得静默合并；
  3. 条件求值为 `unknown` 时不得视为通过；
  4. 无 final 或多 final 无 composition → `unresolved`；
  5. 任一生效 veto → `blocked`；
  6. `blocked`/`unresolved` 必须沿 escalation 处理。
- composition 求值：`priority`（有序归约）/ `joint`（AND）/ `threshold`
  （N of M）确定性归约。
- escalation 触发：`on = unresolved | condition_breached | exception` ×
  `mode = arbitrate | transfer | advise`。
- `unresolved` 语义：**不得隐式选胜者**（不随机 / 不 last-writer /
  不职位高者 / 不先写入者）。
- 三查分离（Validate 阶段同时检查）：
  1. Capability：调用者能否调用此工具（OperationPolicy）；
  2. Authority：调用者是否有权作出该决策（本任务）；
  3. Gate：流程是否完成必要审批。
  三者不可互相替代——`content.final` 不豁免 `OperationPolicy` 的
  approval。

## 产出
- AuthorityResolution 算法 spec + 内核模块（`authority.py`，与
  `tool_manifest.py` 平行）。
- `DecisionClaim` / `AuthorityGrant`（7 元组）模型 + 求值函数。
- 与 `simulation.py` Phase 6（Validate）/ Phase 8（Commit）接点定义。

## 实现状态（2026-08-18，独立推进）
- ✅ 核心已实现为独立模块 `src/my_team/authority.py`（零内核依赖）：
  Domain 8 枚举、AuthorityGrant 7 元组（含 JSON 字符串强制转换）、
  DecisionRequest/Result/Claim、resolve/resolve_claim/claim_overall、
  check_delegation_monotonic（不变量 4 静态半）。
- ✅ 语义锁定（v1，已写入模块 docstring）：裁决按 domain 聚合所有
  subject 的 grant（subject 非过滤器，veto/竞争 final 无条件适用）；
  requester 持 final → ALLOWED，否则 WAITING（对应用人审批流）；
  requester 自身同意隐含；priority 平局 → UNRESOLVED。
- ⏳ 待办：与 simulation.py Validate/Commit 的接点（依赖 process-model
  落地，因 DecisionClaim 需 process_instance_id）。

## 验收标准
- [ ] 多 final 无 composition → `unresolved`（不随机、不 last-writer）
- [ ] 任一生效 veto → `blocked`
- [ ] context 匹配确定性（specificity 高者胜；同层冲突报错不静默）
- [ ] 条件求值 `unknown` 不视为通过
- [ ] 委派单调性动态校验：子 Agent 有效 authority ⊆ 委派者 authority
- [ ] `content.final` 不能绕过 OperationPolicy 的 approval
- [ ] 新测试覆盖以上；`uv run pytest -q` 全绿
