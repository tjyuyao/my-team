---
kind: task
phase: v0.11 扩展表面
source: 审阅 P0-4、§五；OPEN_ISSUE 发布层
priority: high
---

# ExecutionProfile：不可变运行时语义版本绑定 + effective_tick 发布

> **状态（2026-08-24 三态收敛）**：ProcessInstance 废弃后，本卡
> **缩化为"知识/策略快照戳"**——schema 并入 **N4 记忆与注入**
> （条目版本链 + 注入版本戳），绑定语义并入 **N5 任务治理绑定**
> （任务/决策记录生效版本）。本卡保留为历史与设计依据。


## 目标
ProcessInstance 依赖的不止 ProcessDef，还有 Role/Authority/ToolManifest/
OperationPolicy/Skill/KB/Record schema/审批/路由/KPI/Integration。只绑
ProcessDef 版本会破坏可重放性与审计解释性。本任务引入不可变的
`ExecutionProfile`，ProcessInstance 绑定整个 profile。

## 要求 / 规则
- `ExecutionProfile`（不可变，字段齐）：
  `package_id / package_version / process_version / role_version /
  authority_version / capability_snapshot / record_schema_version /
  skill_versions / policy_version`。
- ProcessInstance 绑定 `execution_profile_ref`，而非单 ProcessDef 版本。
- 发布激活支持 `effective_tick`（优先），而非仅 wall-clock；若允许
  wall-clock 必须转为**已确定的 tick 边界**并记录转换结果。
- 包安装拆为多阶段：`Upload → Verify → Static validate → Stage →
  Prepare migration → Activate → Route`；仅 `Activate` 改变运行时可见
  配置；区分"安装成功"与"生效成功"。
- 所有可引用实体用稳定全限定 ID：`package_id:entity_type:entity_id@version`
  （工具名/role 名/record schema/Skill 冲突处理见 package-trust-boundary）。

## 产出
- `ExecutionProfile` 模型 + 绑定规则 spec。
- `effective_tick` 发布语义（schema + 转换规则）。
- 包安装状态机 spec（Activate 为唯一运行时可见切换点）。

## 验收标准
- [ ] ProcessInstance 全部运行时语义绑定到一个不可变 ExecutionProfile
- [ ] 新 PackageVersion 发布不改变既有 ProcessInstance 结构
- [ ] 角色变化不伪造/覆盖 Principal Identity
- [ ] `effective_at`（wall-clock）转换为确定 tick 边界且结果被记录
- [ ] 最小测试向量段：版本绑定 + 灰度路由 通过
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
