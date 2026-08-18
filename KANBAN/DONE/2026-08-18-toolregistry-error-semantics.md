---
kind: task
status: completed
phase: 6 - System Integration
source: "review #11; report §7 P2"
priority: medium
---

# ToolRegistry Error Semantics


## 目标

区分权限错误和工具执行错误，添加 `error_code` 字段。

## 背景

当前 `ToolResult(success=False, error="...")` 无法区分：

- `permission_denied` — Agent 无权限使用该工具
- `tool_error` — 工具执行失败
- `not_found` — 工具未注册

## 要求

1. `ToolResult` 新增 `error_code: str` 字段
2. 权限错误设置 `error_code="permission_denied"`
3. 权限错误设置 `retryable=False`
4. 考虑权限错误是否应抛出 `ToolPermissionError` 而非返回 `ToolResult`
5. 权限错误产生审计事件

## 产出

- [ ] 修改 `agent_runtime.py` 的 `ToolResult` 和 `ToolRegistry.execute`
- [ ] 添加权限错误审计事件
- [ ] 更新相关测试

## 验收标准

- [ ] 权限错误有明确的 `error_code`
- [ ] 权限错误不可重试
- [ ] 权限错误产生审计事件
