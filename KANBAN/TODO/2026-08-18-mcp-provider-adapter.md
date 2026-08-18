---
kind: task
phase: v0.11 扩展协议
source: SPEC §6.5；用户定位补充（个体户/一人公司 + 开发者生态）
priority: medium
---

# v0.11-16: MCP Provider Adapter（MCP server → ToolManifest）


## 目标
把 MCP server 暴露的工具接入内核，自动生成 ToolManifest，并走
现有 pending op / ToolRequest / ToolResultContract 路径执行；
身份字段仍由内核注入，MCP 工具默认拒绝。

## 要求 / 规则
- 支持 MCP 传输：stdio（本地子进程）优先；HTTP/SSE 可作为后续。
- MCP Adapter 启动时枚举 tools/resources，将 MCP tool schema
  映射为 ToolManifest（name/version/input_schema/output_schema）。
- 执行器注册：
  - 本地 stdio → UNTRUSTED_OUT_OF_PROCESS；
  - 远程 HTTP → EXTERNAL_IRREVERSIBLE（需幂等与状态回查）。
- 调用经 ToolRequest/ToolResultContract；agent_id/task_id/
  state_epoch/manifest_hash 由内核注入。
- 默认 deny-by-default：MCP 工具必须显式加入 OperationPolicy
  allowlist 后才可使用。
- 适配器负责超时、限流、重试与结果契约转换。
- MCP resources 可映射为 SharedKB 只读条目或 AssetStore 引用。

## 产出
- MCP Adapter 模块与注册 API。
- 一个假 MCP server 的集成测试（tools 枚举、调用、限流、默认拒绝）。

## 验收标准
- [ ] 注册一个 MCP server 后，其 tools 自动出现在 ToolRegistry
- [ ] MCP tool 未被 allowlist 时 Agent 调用被 POLICY_DENIED 拒绝
- [ ] allowlist 后 Agent 可调用 MCP tool，结果经 ToolResultContract 返回
- [ ] MCP server 无法伪造 agent_id（内核注入字段生效）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
