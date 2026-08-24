---
kind: task
phase: v0.11 扩展表面
source: 原 E1+E2 缩（ProcessDef 废弃，2026-08-24 坍缩）；SPEC §4.2/§10.2
priority: high
---

# 任务治理绑定：HumanTask + escalation 归一 + Authority 接点


## 目标
在 Task 设备（N1）与岗位模型（N2）之上把"治理"接到任务与效果：
HumanTask 标准化、escalation 单一 canonical schema、Authority 裁决
接入 Validate/Commit（DecisionClaim 挂 task_id）、三查分离落地
（SPEC §10.2）。原 process-model（ProcessDef/ProcessInstance/Gate
状态机）已废弃——编排实例载体 = Task 树，流程 = SOP 知识。

## 要求 / 规则
- HumanTask：`kind = work | approval | decision | consultation`；
  审批 = 建任务 → Email 通知 → 人类经 UI/邮件（IngressEvent,
  source="human"，T12a 路径）回应 → 决定续延方向；复用该注入路径，
  **无旁路注入口**；
- **escalation 归一**：canonical schema = `authority.Escalation`
  （on × mode）扩展 `target`；T12a 升级邮件、审批超时、Authority
  unresolved/exception 升级共用同一 schema 与审计事件类型——代码中
  不存在第二种 escalation 表示；
- **Authority 接点**：DecisionClaim 挂 `task_id`；裁决在 Validate
  得出 binding，Commit 动态重检；context 匹配/composition/
  escalation 沿用 `authority.py` 核心（8 域/32 测试已绿，零内核
  依赖）；委派单调动态校验（不变量 4）；
- **三查分离**（Validate 同时检查，互不替代）：Capability
  （OperationPolicy）/ Authority（裁决）/ 审批态（HumanTask 状态）；
  `content.final` 不豁免 OperationPolicy approval；
- **知识/策略快照戳**：任务与决策记录生效的 skill/tool/policy
  版本（原 E4 缩化；与 N4 注入可重放联测）；
- escalation 沿 superior 边（组织架构设备默认声明，§4.1）。

## 产出
- HumanTask 模型 + 审批流（复用 T12a IngressEvent 路径）；
- authority.py 接入 simulation.py Validate/Commit（DecisionClaim 挂
  task_id）；
- escalation 单一 schema（三处统一）+ 审计事件类型；
- 快照戳落地（任务/决策 → 生效版本）。

## 验收标准
- [ ] 审批经 HumanTask + IngressEvent 注入（无旁路注入口）
- [ ] 代码中不存在第二种 escalation 表示（canonical schema 唯一）
- [ ] DecisionClaim 裁决在 Validate 出 binding、Commit 重检；
      委派单调动态校验
- [ ] `content.final` 不豁免 OperationPolicy approval（三查互不替代）
- [ ] 任务/决策记录快照戳（skill/tool/policy 版本）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
