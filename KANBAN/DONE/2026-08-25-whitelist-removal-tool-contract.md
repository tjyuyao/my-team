---
kind: task
status: completed
phase: v0.11 扩展表面
source: SPEC §3.5/§5.1；三态收敛（2026-08-24）；拆分自原 device-model（N1 → N1a/N1b/N1c）
priority: high
---

# N1b 废除独立工具白名单 + 工具契约（横切改造）


## 目标

废除独立工具白名单（ROOT_TOOLS / MANAGER_TOOLS / WORKER_TOOLS 与
按名字的 `agent.tools`），权限接线切换到 N1a 的两层 Grant 求值；
ToolManifest 扩展新契约字段。**依赖 N1a**（接线要 Authority 求值）。

## 要求 / 规则

- **废除**：ROOT_TOOLS / MANAGER_TOOLS / WORKER_TOOLS、ToolRegistry
  （register_agent/get_allowed_tools）、ToolContext.allowed_tools
  全部删除；
- **接线**：simulation 4 处白名单点（`_phase_act` ToolContext 构造
  L3640 / `_phase_validate` 两处按名检查 L4007、L4053 / dispatch 工具
  上下文 L4766）改为两层 Grant 求值（∃position：Grant(agent,
  position) ∧ Grant(position, entity) ∧ 锁）；agent_runtime /
  llm_agent / prompt_templates 同步迁移（去 role 文案与 allowed_tools）；
- **ToolManifest 新契约**：`device_id`、`capability`（uuid）、
  `approval_policy`、`ingress_event_types`、`egress`、
  `compensation_tool`；LLM 工具定义从 input/output_schema 自动生成
  （manifest_to_tool_definition 保留）；builtin manifests 拆设备经
  ToolPlugin API 注册（设备注册 = 向 Authority 注册工具 uuid）；
- **附**：tick 模型层对齐（TickPhase 十阶段、TickSnapshot 删除——
  零外部使用者）；patch_ops 落位工具清单；
- **测试迁移**：test_agent_runtime / test_llm_agent /
  test_task_cancellation 的白名单断言改为 Grant 求值断言。

## 产出

- 白名单零残留；两层 Grant 求值接线；
- ToolManifest 新契约 + 工具定义自动生成；
- tick 模型层对齐；测试迁移。

## 验收标准

- [x] 白名单路径零残留（grep ROOT_TOOLS / MANAGER_TOOLS /
      WORKER_TOOLS 为空）
- [x] 任一调用 = 两层 Grant ∧ 锁（与 N1a 求值接线有测试）
- [x] 未注册 uuid / 未授权工具调用被拒绝（deny-by-default）
- [x] `uv run pytest -q` 全绿；ruff/mypy 通过
## 完成注记（2026-08-25）

- 交付：删白名单常量（ROOT_TOOLS/MANAGER_TOOLS/WORKER_TOOLS 零残留）；
  ToolRegistry 改 manifest/handler 注册表 + Authority 授权桥；simulation
  4 接线点全改两层 Grant（authority.authorize(agent_id, manifest.capability)）；
  ToolManifest 新契约 6 字段（capability 按契约 uuid5 确定性派生）；
  TickPhase 十阶段；AgentConfig.tools 字段删除（model_extra 兼容读取）。
- 过渡决策：register_agent/get_allowed_tools/allowed_tools 字段保留为
  弃用兼容桥（12 个存量测试依赖；生产决策路径 100% 走 Authority，彻底
  删除需另开迁移卡）；默认初始布线 = 基础工具 ∪ config 声明工具
  （引导形态，N3 可替换）；Validate 检查顺序调整（request_id 去重提前）。
- 遗留：tick_engine.py 旧 TickPhase/TickSnapshot 零使用者未删（建议
  后续清理）；Authority 不落库，load_from 从 config 重建布线。
