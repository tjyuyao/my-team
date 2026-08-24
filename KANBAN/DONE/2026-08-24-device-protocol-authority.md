---
kind: task
status: completed
phase: v0.11 扩展表面
source: SPEC §1.8/§3.5/§5.1；三态收敛（2026-08-24）；拆分自原 device-model（N1 → N1a/N1b/N1c）
priority: high
---

# N1a 设备协议与 Authority（注册中心 + 布线 + 两层 Grant）— 纯新增地基


## 目标

立设备协议与 Authority 的**新地基**（纯新增，不碰现有代码路径）：
Device 一般结构（数据 + 工具 + 受控 uuid 注册）+ Authority（注册中心
+ 布线中心 + 能力=权限+记忆）+ 配置设备（授予/策略/限额数据）。
position 在本卡为**裸 uuid**，本体由 N2 提供。

## 要求 / 规则

- **Device 一般结构 = 数据 + 工具**：数据任意内部结构，每个需独立
  权限控制的条目/范围带 uuid；工具可分组为工具包，需权限控制的
  工具包/单工具带 uuid；设备**动态向 Authority 注册**这些受控 uuid；
  设备依赖用接口定义；
- **设备协议三条（2026-08-24 定案）**：
  - 设备**不维护账本**：只持当前状态（effect 应用直接改状态），
    重放源唯一 = Journal（§5.9）；
  - **身份落字段是设备职责**：设备工具把调用上下文身份（agent_id +
    position_ref，由内核构造的 ToolContext 绑定，§3.5）落为自己的
    数据字段（邮件 from、任务 assignee 等）；
  - **注册即声明注入内容**：注册受控 uuid 时声明授予生效后注入
    记忆的 content（引导 Agent 使用，如页面权限说明——非数据全量）；
    授权查 Authority，注入内容的解释权在设备内部；
- **Authority（特殊 Device，每 Team 仅一个，Owner 安装/替换）**：
  - 注册中心：接收所有设备的受控 uuid 注册；
  - 布线中心：把 Agent 与 Device 经 position 布线——`Grant(agent,
    position)`（成员）+ `Grant(position, entity_id)`（能力，entity_id
    ∈ 注册的 uuid）；deny-by-default；effect = allowed / denied /
    requires_approval；
  - 基类行为（能力 = 权限 + 记忆）：授予生效 → 注入设备声明的
    content（注入接线接口就绪，N4 联测）；grant 带 priority（<10
    固定工作记忆 / ≥10 触发召回，N4）；
  - 本身是 Device（可自注册、可为特定 position 提供 memory entry）；
    引导 = org 初始化（场景包携带 Authority 子类）安装 + 初始授予集；
  - **组织架构 = Authority 子类**（N3）。
- **配置设备**：`Grant(position, entity_id)` 与 priority、allowlist、
  审批配置、限额参数（含容量参数 §3.8）。

## 产出

- Device 协议（数据 + 工具 + uuid 注册接口）；
- Authority 设备（注册中心 + 布线中心 + 两层 Grant 求值 + 单例/Owner）；
- 配置设备（授予/策略/限额数据面）；
- 注入接线接口（授予 → 设备声明 content，N4 联测钩子）。

## 验收标准

- [x] 设备注册受控 uuid 至 Authority；未注册 uuid 无法被授予
- [x] 任一调用 = ∃position：Grant(agent, position) ∧ Grant(position,
      entity) ∧ 锁（求值路径有测试）
- [x] Authority 每 Team 单例强制；安装/替换仅 Owner
- [x] 注册即声明注入内容（content 声明 + 注入接线接口有测试）
- [x] `uv run pytest -q` 全绿（1032 passed）；ruff/mypy 通过

## 完成注记（2026-08-24）

- 交付：`src/my_team/devices/`（base.py 设备协议 / authority.py /
  config.py 配置设备）+ tests（test_device_protocol.py 6 +
  test_authority_device.py 20）；
- 设计要点：`replace` 用 `register=False` 候选 + Owner 原子接管
  （组织架构可替换 N3 前置）；`injection_for` 严格实现"外加载记忆
  条目必然对应一条 (position, entity_id) 授予"（N4 联测钩子）；
- 遗留：锁约束（∃position ∧ Grant ∧ **锁**）由 N1b 接线叠加；
  position 本体由 N2 提供（本卡用裸 uuid）。
