---
kind: task
phase: v0.11 扩展表面
source: SPEC §1.8/§3.5/§5.1；三态收敛（2026-08-24）
priority: high
---

# 设备协议与 Authority（注册中心 + 布线 + 两层 Grant）


## 目标
把"设备"落成正式协议：**设备 = 数据 + 工具**（带 uuid 注册机制），
并实现 **Authority**——每 Team 唯一、Owner 安装的特殊 Device：
注册中心 + 布线中心 + 能力=权限+记忆。权限从"独立工具白名单"改为
两层 Grant（SPEC §1.8/§5.1）。

## 要求 / 规则
- **Device 一般结构 = 数据 + 工具**：数据任意内部结构，每个需独立
  权限控制的条目/范围带 uuid；工具可分组为工具包，需权限控制的
  工具包/单工具带 uuid；设备**动态向 Authority 注册**这些受控
  uuid；设备依赖用接口定义（如邮箱设备依赖凭证设备接口）；
- **Authority（特殊 Device，每 Team 仅一个，Owner 安装）**：
  - 注册中心：接收所有设备的受控 uuid 注册；
  - 布线中心：把 Team 内所有 Agent 与所有 Device 经 position 布线
    ——`Grant(agent, position)`（成员）+ `Grant(position,
    entity_id)`（能力，entity_id ∈ 注册的 uuid）；deny-by-default；
    effect = allowed / denied / requires_approval；
  - **基类行为（能力 = 权限 + 记忆）**：授予生效 → 设备的数据与
    工具**注入 agent 记忆**（外加载记忆条目必然对应一条
    `(position, entity_id)` 授予，N4 联测）；grant 带 priority
    （<10 固定工作记忆 / ≥10 触发召回，N4）；
  - 本身是 Device（可自注册、可为特定 position 提供 memory
    entry）；引导 = org 初始化（场景包携带 Authority 子类）安装 +
    初始授予集；
  - **组织架构 = Authority 子类**（N3）。
- 归位为设备：基础设备（SharedKB/邮箱/RecordStore/AssetStore/
  CredentialStore）、**Task 设备**（任务树公共数据 + 细粒度：按
  position 求值）、**世界记忆设备**（Journal 持久化与查询）、
  **配置设备**（`Grant(position, entity_id)` 与 priority、allowlist、
  限额参数）；Ingress/Integration 为外部世界设备；
- **废除独立工具白名单**：ROOT_TOOLS/MANAGER_TOOLS/WORKER_TOOLS
  与按名字的 `agent.tools` 不再存在；
- ToolManifest：`device_id`、`capability`（uuid）、`approval_policy`、
  `ingress_event_types`、`egress`、`compensation_tool`；
  ToolPlugin API 注册 = 设备向 Authority 注册工具 uuid；
- OperationPolicy 继续 deny-by-default（机制在内核，allowlist/审批
  数据归配置设备）；锁原语在内核、锁实例在设备；
- 预算拆分：LLM API 限额归 Agent 引擎内部（N4 侧）；外部资源限额
  与 Ingress/Integration 设备一起管理（速率与背压）。

## 产出
- Device 协议（数据+工具+uuid 注册）+ 各设备实现（现有 store 适配）；
- Authority（注册中心 + 布线 + 两层 Grant 求值路径）；
- 能力=权限+记忆的注入接线（授予 → 记忆条目，N4 联测）；
- Task 设备；世界记忆设备（Journal 落位）；配置设备。

## 验收标准
- [ ] 设备注册受控 uuid 至 Authority；未注册 uuid 无法被授予
- [ ] 任一调用 = ∃position：Grant(agent, position) ∧ Grant(position,
      entity) ∧ 锁；白名单路径无残留
- [ ] 授予生效后设备的对应数据/工具注入 agent 记忆（有测试）
- [ ] Authority 每 Team 单例强制；安装/替换仅 Owner
- [ ] Journal 持久化/查询经世界记忆设备接口；内核无数据直连
- [ ] 预算拆分生效（LLM 限额 Agent 内、外部速率 Ingress）
- [ ] 设备依赖经接口声明（无跨设备直连）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
