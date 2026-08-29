---
kind: task
status: completed
phase: v0.14
source: SPEC.md
priority: high
---

# 设备私家与维护会话设计修订

## 目标

删除共享 `data/devices` 源码区及其框架语义。设备 identity 创建时拥有
`data/<device-id>/` 私家，设备实现与状态均属于该私家；不支持
`INSTANCE=per-agent`、`bound_agent` 或 `device-id@agent-id` 实例。

为设备维护者定义最小额外能力：仅能对被授权的目标设备执行卸载、在设备
卸载期间编辑其私家实现、再请求重载；不得获得 root、Authority 数据或其它
身份私家权限。运行中实现文件只读。

## 验收

- 运行时、bootstrap、示例与文档不再依赖或识别 `data/devices`；
- 设备加载路径只来自设备 identity 私家，重载使用该私家快照；
- 不存在 per-agent device、`bound_agent` 或 `device-id@agent-id` 语义；
- 维护会话的挂载范围只含维护者自身家与获授权目标设备家，且目标设备
  必须已卸载；
- 故事测试覆盖卸载→修改→重载，以及越权访问其它身份家的拒绝；
- 该卡完成后，v0.14 才可进入 focused tests、acceptance、milestone 与归档。

## 完成记录

- 2026-08-29: 全部验收条件满足
  - `data/devices` 引用已从 `src/` 移除（仅历史 KANBAN 文档提及）
  - 设备加载路径统一为 `data/<identity>/device.py`
  - `INSTANCE`/`bound_agent`/`device-id@agent-id` 语义已删除
  - `_maintenance_session` 入口实现：权限裁决（`_authorize_device`）+
    目标卸载状态检查 + 双锚点挂载（维护者家 + 目标设备家）
  - 故事测试通过：`test_maintenance_session_unload_edit_reload`（14 passed）

## 依赖

`KANBAN/PLAN/v0.14.0-sandbox`
