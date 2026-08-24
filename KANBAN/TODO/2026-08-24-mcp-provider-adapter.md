---
kind: task
phase: v0.11 扩展协议
source: SPEC §6.5（2026-08-24 补信任框架）；用户定位补充（个体户/一人公司 + 开发者生态）
priority: medium
---

# MCP Provider Adapter（MCP server → ToolManifest）


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
- **安装框架（2026-08-24 修订为审计制）**：Adapter 是可执行能力包，
  经 `INSTALL_PACKAGE` 审计安装（E5/N9：如实申报 + 安装审计 + 审计员
  通知）；方向不变：deny-by-default、内核注入 agent_id。
- **设备入口（2026-08-24 三态收敛）**：MCP 是**外部能力设备**的
  接入方式——MCP server 暴露的工具映射为设备能力（ToolManifest
  device_id/capability），经设备 ACL 与授权使用。
- **E3 挂接**：远程 HTTP 执行器（EXTERNAL_IRREVERSIBLE）的
  unknown/对账语义挂接 pending-outbox-recovery（E3）；幂等键用
  稳定键，不用随机后缀。

## 产出
- MCP Adapter 模块与注册 API。
- 一个假 MCP server 的集成测试（tools 枚举、调用、限流、默认拒绝）。

## 验收标准
- [ ] 注册一个 MCP server 后，其 tools 自动出现在 ToolRegistry
- [ ] MCP tool 未被 allowlist 时 Agent 调用被 POLICY_DENIED 拒绝
- [ ] allowlist 后 Agent 可调用 MCP tool，结果经 ToolResultContract 返回
- [ ] MCP server 无法伪造 agent_id（内核注入字段生效）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
