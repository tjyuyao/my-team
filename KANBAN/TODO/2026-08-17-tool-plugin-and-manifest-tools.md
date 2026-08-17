# v0.10-7: 公共工具插件 API 与 manifest 自动生成工具定义

**Phase:** v0.10 能力
**Source:** SPEC §6.2；OI-004 §3.1
**Priority:** medium

## 目标
场景包可以在不修改内核代码的前提下注册新工具；LLM 工具定义
从 ToolManifest 自动生成，删除手写工具表。

## 要求 / 规则
- 提供 `Simulation.register_tool(manifest, handler, executor=None,
  policy=None)` 公共 API；handler 签名 `(context: ToolContext,
  **args) -> ToolResult`。
- handler 不得访问 Simulation 私有成员；通过注入的 subsystem
  handles 访问文件/KB/Record/邮件。
- LLM 工具定义由 `ToolManifest.input_schema/output_schema`
  生成 JSON Schema；`PromptTemplates.render_tool_definitions`
  改为调用生成器。
- 注册即校验：manifest 合法、name 唯一、policy 默认拒绝。
- 覆盖 v0.8 已内置 12 个工具（read/ls/write/kb_write/send_email/
  delegate/apply_patch/run_tests/git_diff/git_status/
  python_compute/python_transform）的自动定义。

## 产出
- 公共 register_tool API 与 schema→tool-definition 生成器。
- 内置工具迁移到插件化注册路径。

## 验收标准
- [ ] 测试中注册自定义工具后 Agent 可调用，且未改 simulation.py
- [ ] LLM 工具定义包含全部已注册工具（非手写 6 个）
- [ ] 无 manifest 工具在 policy 启用时被拒绝
- [ ] 新增测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
