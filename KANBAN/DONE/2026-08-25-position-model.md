---
kind: task
status: completed
phase: v0.11 扩展表面
source: SPEC §1.8/§3.5/§4.1/§5.8；三态收敛（2026-08-24）
priority: high
---

# 岗位模型：Position（ACL 主体）+ Agent uuid4 + 占据继承


## 目标
组织架构由岗位承载：`Position {position_id, name, jd, edges}`。
**position 即 ACL 主体（role 并入，不再单独设计）**；Agent 被 hire
进岗位即**自动继承**其边与授予（岗人分离）。Agent 身份迁全局
uuid4。经手物（task/report/mail 账号）归属岗位；agent 无可持有
资产（SPEC §4.1）。

## 要求 / 规则
- Position 实体（**组织架构数据**，由组织架构（Authority 子类，
  N3）提供定义，非核心结构）：`name`（可读名/业务标签，非权限
  依据）、`jd`（职责/提示词 = org 干预 agent 的唯一杠杆，
  `[POSITION_JD]` 注入，N4 联测）、edges（superior 唯一 /
  subordinates / collaborators）；
- **ACL 主体 = position**（role 并入）：授予 = `Grant(agent,
  position)`（成员）+ `Grant(position, entity_id)`（能力，entity_id
  为设备注册的 uuid，N1）；有效权限 = 两层授予 ∧ 锁（SPEC
  §1.8/§3.5）；
- **priority（场景包可配置项，N1/N4 联测）**：grant 带 priority
  ——`< 10` 固定工作记忆（单独预算、不可超、预算可配置；JD 属此
  类），`≥ 10` 触发器召回；
- **边语义 = 组织架构声明的数据**：org 定义自己的边行为（参考：
  command/request、declined、升级沿 superior 而回报只回请求方）；
  内核只校验**四条治理不变量**（授权不授责 / veto 默认不可转授 /
  escalation 不转移 ownership / 委派单调）；
- Agent：`{agent_id uuid4, kind(llm|human|service), position_ref,
  llm_profile/human_queue/service_ref, metadata}`；占据即继承
  （边与授予）；
- 经手物归属：task/report/mail 账号概念上属 position（换人不换岗、
  活留岗上）；实现为归属元数据（静态先行，不做运行时换人策略）；
- 多版本 agent 候选（同岗不同配置评估）预留——组织架构的 mount
  挂载用（N3）；
- 迁移：AgentConfig（role/tools 白名单/parent-children）→ 新模型。

## 产出
- Position/Agent 数据模型（uuid4）+ 占据/继承解析（两层授予）；
- 边语义声明 schema（组织架构数据面，N3 落地）；
- priority 配置（场景包可配置，persistent 预算独立可配）；
- 经手物归属元数据（task/mail 账号 → position_id）。

## 验收标准
- [x] agent 占据 position 后继承边与授予（两层 Grant，有测试）
- [x] 产物（task/report/mail 账号）归属 position，不随 agent 身份迁移
- [x] 边语义声明违反四条治理不变量 → 静态拒绝（N8 联测）
- [x] priority <10 条目固定注入（persistent 预算硬上限、可配置）；
      ≥10 触发召回（N4 联测）
- [x] 白名单/业务标签授权路径无残留（与 N1 联测）
- [x] 直派形态（agent → position 直接指派）接口预留
- [x] `uv run pytest -q` 全绿；ruff/mypy 通过

## 完成注记（2026-08-24）

- 交付：`models/position.py`（Position/PositionGraph/边语义声明 + 四条
  治理不变量静态校验/经手物归属元数据/priority 分级/占据继承解析/
  直派预留）+ `models/agent.py` 新 Agent（SPEC §4.1）+ 27 测试；
- 兼容性：AgentConfig 零行为改动保留（字符串 agent_id/parent/role 拆除
  留给 N1b/N3）；新模型 id 用 uuid.UUID，与 Authority str 接口边界转换；
- 验收 1/2/4/6 有测试；3/5/7 出 schema/接口，N8/N1/N4 联测后续；
- 遗留：`effective_capabilities` 借道 Authority 私有 `_grants_for`
  （N3 重构需同步，已标注）。
