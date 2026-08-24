---
kind: task
phase: v0.11 扩展表面
source: SPEC §4.1/§4.2；三态收敛（2026-08-24）
priority: high
---

# 岗位模型：Position（JD/边/授权）+ Agent uuid4 + 占据继承


## 目标
组织架构由岗位承载：`Position {position_id, jd, edges, roles}`；
Agent 被 hire 进岗位即**自动继承**其关系与 roles（岗人分离）。
Agent 身份迁全局 uuid4。经手物（task/report/mail 账号）归属岗位；
agent 无可持有资产（SPEC §4.1）。

## 要求 / 规则
- Position 实体（**组织架构设备的数据**，非核心结构）：`jd`
  （职责/提示词 = org 干预 agent 的唯一杠杆，`[POSITION_JD]`
  注入，N4 联测）、edges（superior 唯一 / subordinates /
  collaborators）、**roles（多对多：岗位可具有多个 role，并集
  生效）**；
- **ACL 主体 = role（内核实体 `{role_id, name}`，零行为语义，
  N1 联测）**：role 是一组权限绑定的命名主体（经典 ACL 用户组
  语义——一个 role 对应多个 agent）；有效权限 = 岗位 roles 并集
  的 grants ∧ 设备 ACL ∧ 锁（SPEC §1.8）；
- **业务标签不构成权限**：position.name/display 仅路由元数据；
- **直派形态预留**：不经组织架构、直接给 agent 指派 role 的
  "agent grants 设备"是合法替代（框架不依赖组织架构存在）；
- **边语义 = 组织架构设备声明的数据**：org 定义自己的边行为
  （参考语义：command/request、declined、升级沿 superior 而回报
  只回请求方）；内核只校验**四条治理不变量**（授权不授责 / veto
  默认不可转授 / escalation 不转移 ownership / 委派单调）；
- Agent：`{agent_id uuid4, kind(llm|human|service), position_ref,
  llm_profile/human_queue/service_ref, metadata}`；占据即继承；
- 经手物归属：task/report/mail 账号概念上属 position（换人不换岗、
  活留岗上）；实现为归属元数据（静态先行，不做运行时换人策略）；
- 多版本 agent 候选（同岗不同配置评估）预留——组织架构设备挂载用
  （N3）；
- 迁移：AgentConfig（role/tools 白名单/parent-children）→ 新模型。

## 产出
- Role（内核实体）/Position/Agent 数据模型（uuid4）+ 占据/继承
  解析（roles 并集）；
- 边语义声明 schema（组织架构设备数据面，N3 落地）；
- 经手物归属元数据（task/mail 账号 → position_id）。

## 验收标准
- [ ] agent 占据 position 后继承边与 roles（并集生效，有测试）
- [ ] 产物（task/report/mail 账号）归属 position，不随 agent 身份迁移
- [ ] 边语义声明违反四条治理不变量 → 静态拒绝（N8 联测）
- [ ] 白名单/业务标签授权路径无残留（与 N1 联测）
- [ ] 直派形态（agent → role 直接指派）接口预留
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
