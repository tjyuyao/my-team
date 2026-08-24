---
kind: task
phase: v0.11 扩展表面
source: SPEC §4.1/§6.6/§13；三态收敛（2026-08-24）
priority: high
---

# 组织架构设备 + Human UI 内核化与设备插件


## 目标
组织调整 = **设备操作**：关系图/岗位/授权的读改写是**组织架构设备**，
root 级 agent 持该设备权限即可运行时换人/调整组织（**动态优于
静态**）。Human UI 系统（Control Plane）归内核，设备可扩展前后端
模块插件（SPEC §6.6）。

## 要求 / 规则
- 组织架构设备：positions/relations/grants/边语义声明的读改写能力
  （GET/PUT positions、改边、mount/换人）；root 授权经
  `(组织架构设备, capability)`；
- 岗人分离动态：mount 多版本 agent 候选到岗位（评估用；实现静态，
  接口预留——N2 的预留在此落地）；
- Control Plane 内核化：通用操作台（启停/消息/审批/审计/看板）属
  内核（纯逻辑 + 框架，不持有业务数据）；设备可注册 UI 插件
  （前端模块 + 后端 handler），经设备接口声明；
- 组织调整全程入 Journal（审计、可回滚）；改边/改授权触发不变量
  校验（四条治理不变量，N8 联测）。

## 产出
- 组织架构设备实现（岗位/关系/授权读改写能力）；
- Control Plane 内核模块 + 设备 UI 插件注册机制；
- mount（岗人分离）接口。

## 验收标准
- [ ] 持权限的 agent 可读改写岗位/边/授权；无权限者 POLICY_DENIED
- [ ] 组织调整入 Journal 可审计；违反不变量被拒
- [ ] 设备注册 UI 插件后 Control Plane 渲染对应模块（有测试）
- [ ] mount 接口可用（静态版本；动态评估接口预留）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
