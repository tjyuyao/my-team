---
kind: task
phase: v0.11 扩展表面
source: SPEC §3.7/§5.1/§5.8/§10；三态收敛（2026-08-24）
priority: high
---

# 组织架构（Authority 子类）+ Human UI 内核化与设备插件


## 目标
组织架构 = **Authority 子类**（§5.1）：提供上下级关系、JD 等
memory entry；边语义 = 它注册的工具能力 + 生效条件。组织调整 =
设备操作（root 级 agent 持该设备权限即可运行时换人/调整组织——
**动态优于静态**）。Human UI 系统（Control Plane）归内核，设备可
扩展前后端模块插件（SPEC §3.7）。

## 要求 / 规则
- 组织架构（Authority 子类）：Position（JD/边）+ 边语义声明 +
  上下级关系与 JD 作为 memory entry（priority <10 固定注入，
  N2/N4 联测）；授予数据 `Grant(position, entity_id)` 在配置设备
  （N1）；
- 能力：读改写岗位/边/授权、mount（岗人分离挂载）；root 授权经
  `Grant(root_position, 组织架构实体)`；
- 岗人分离动态：mount 多版本 agent 候选到岗位（评估用；实现静态，
  接口预留——N2 的预留在此落地）；
- **可替换**：组织架构是可替换的 Authority 子类——朴素系统可用
  直派 Authority（不经组织架构直接指派 position），不改变内核与
  Agent 模型；
- Control Plane 内核化：通用操作台（启停/消息/审批/审计/看板）属
  内核（纯逻辑 + 框架，不持有业务数据）；设备可注册 UI 插件
  （前端模块 + 后端 handler），经设备接口声明；
- 组织调整全程入 Journal（审计、可回滚）；改边/改授权触发闭包
  不变量校验（四条治理不变量，N8 联测）。

## 产出
- 组织架构（Authority 子类）实现（岗位/边/授权读改写 + 关系与
  JD 的 memory 注入）；
- Control Plane 内核模块 + 设备 UI 插件注册机制；
- mount（岗人分离）接口。

## 验收标准
- [ ] 持权限的 agent 可读改写岗位/边/授权；无权限者 POLICY_DENIED
- [ ] 上下级关系与 JD 作为 memory entry 注入占据者（priority<10）
- [ ] 组织调整入 Journal 可审计；违反不变量被拒
- [ ] 设备注册 UI 插件后 Control Plane 渲染对应模块（有测试）
- [ ] mount 接口可用（静态版本；动态评估接口预留）
- [ ] 直派 Authority 可替换组织架构（同一接口，有测试）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
