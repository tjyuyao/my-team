# 编排层 Process Model：ProcessDef / ProcessInstance / Gate schema

**Kind:** task
**Phase:** v0.11 扩展表面
**Source:** 审阅 P0-1、§四.3/§四.4；OPEN_ISSUE 编排层
**Priority:** high

## 目标
定义扩展表面中心对象 ProcessDef 的可执行语义。当前代码只有
`models/task.py`（Task）、`models/continuation.py`、`models/activation.py`、
`models/intent.py`，**无 ProcessDef/ProcessInstance/Gate**。本任务补齐最小但
完整的 Process Model，而非继续堆概念术语。

## 要求 / 规则
- `ProcessDef` schema：`process(id, version, input_schema, output_schema)`
  + `steps(id, executor=role, input/output binding, retry.max_attempts,
  sla.ticks)` + `gate(id, kind=gate, domain, gate_type, authority_ref)`
  + `transitions(from, to, when)`。
- `ProcessInstance` 状态机（成为规范的固定枚举）：
  `created / ready / running / waiting_external / waiting_human /
  waiting_dependency / blocked / succeeded / failed / cancelled /
  compensating / compensated`。
- `ProcessInstance` 数据模型：`instance_id, process_def_ref,
  execution_profile_ref, source_event_id, input_snapshot_ref, state,
  current_nodes, variables_ref, owner_ref, accountability_ref,
  created_tick, updated_tick, deadline_tick, cancellation_policy`。
- 关系澄清（不得混用）：
  - `ProcessInstance` = 流程执行实体；
  - `Task` = 可分派的人类/Agent 工作单元；
  - `Step` = 流程结构节点；
  - `Gate` = 流程裁决节点。
  一个 Step 可创建多个 Task；Task 完成 ≠ Step 成功。
- Gate 拆"参与者选择"与"裁决规则"两半：
  `participants(selector=role, min)` + `decision(mode=unanimous|quorum,
  domain)` + `on_reject(transition)` + `on_timeout(escalation)`；
  gate 引用 `authority_ref`，**不内置裁决**（与 Authority 正交）。
- 统一 HumanTask 模型（吸收现有 ApprovalGate / HUMAN_APPROVAL /
  Human Worker / HumanMessage 的重叠）：`kind = work | approval |
  decision | consultation`。

## 产出
- ProcessDef / ProcessInstance / Gate / HumanTask 的 schema spec（补入
  SPEC 新章节或 OPEN_ISSUE 补充节）。
- 最小 pydantic 模型 + 状态机骨架（放在 `models/` 下新模块）。
- 最小测试向量首段：`IngressEvent → ProcessInstance → assignment`。

## 验收标准
- [ ] ProcessDef schema 可被静态校验（引用完整性：role/step/gate/escalation 存在）
- [ ] ProcessInstance 绑定不可变 `execution_profile_ref`（不裸绑 ProcessDef）
- [ ] gate 拒绝/超时/未决均有终止路径或显式 escalation
- [ ] 无 deadline 的 `waiting_*` 被静态校验拒绝
- [ ] 一个 Step 可产多个 Task，且 Task 完成不自动推进 Step
- [ ] 新测试覆盖状态机迁移；`uv run pytest -q` 全绿
