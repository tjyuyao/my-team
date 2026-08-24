---
kind: task
phase: v0.11 扩展表面
source: SPEC §1.7/§3.2/§4.2/§6.1/§7；三态收敛（2026-08-24）
priority: high
---

# 设备化与单层授权（Device 协议 + (device, capability) 授权）


## 目标
把"设备"落成正式协议：设备 = 数据 + 读写工具 + ACL + 锁（依赖用
接口定义）。现有 store 归位设备；权限从"独立工具白名单"改为单层
`(device, capability)` 授权（SPEC §1.7/§6.1）。

## 要求 / 规则
- Device 协议：`{device_id, capability 集, ACL, 锁, data 契约,
  依赖接口}`；设备依赖用接口定义（如邮箱设备依赖凭证设备接口）；
- 归位为设备：基础设备（SharedKB/邮箱/RecordStore/AssetStore/
  CredentialStore）、**Task 设备**（任务树公共数据 + 细粒度 ACL：
  可见性按关系求值；生命周期状态机 = 设备逻辑）、**世界记忆设备**
  （Journal 持久化与查询）、**配置设备**（role grants 与策略
  配置数据）；Ingress/Integration 为外部世界设备；
- **ACL 主体 = role（内核实体，`{role_id, name}` 零行为语义）**：
  grants（`role → (device, capability)`）与设备 ACL 条目都是数据、
  引用 role；**细粒度 ACL 同样引用 role**——KB 页面级（逐条目
  权限）、Task 级（同一任务对不同 role 可见/可改程度不同）；
- 授权单层化：`有效权限 = role grants ∧ 设备 ACL ∧ 锁`；废除
  ROOT_TOOLS/MANAGER_TOOLS/WORKER_TOOLS 与按名字的 `agent.tools`；
- ToolManifest 加 `device_id`/`capability`；OperationPolicy 继续
  deny-by-default（机制在内核，allowlist/审批数据归配置设备与
  role grants）；
- 预算拆分：LLM API 限额归 Agent 引擎内部（N4 记忆系统侧）；外部
  资源限额与 Ingress/Integration 设备一起管理（速率与背压）；
- 锁原语（令牌/释放验证）在内核，锁实例（持有对象/用途）在设备。

## 产出
- Device 协议 + 各设备实现（现有 store 适配为设备接口）；
- 单层授权检查路径（ToolRegistry 改造，白名单删除）；
- Task 设备（细粒度 ACL + 生命周期）；世界记忆设备（Journal 落位）；
  配置设备。

## 验收标准
- [ ] 任一工具调用 = role grants ∧ 设备 ACL ∧ 锁；白名单路径无残留
- [ ] 细粒度 ACL（KB 页面级 / Task 级）引用 role 生效（有测试）
- [ ] Journal 持久化/查询经世界记忆设备接口；内核无数据直连
- [ ] 预算拆分生效（LLM 限额 Agent 内、外部速率 Ingress）
- [ ] 设备依赖经接口声明（无跨设备直连）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
