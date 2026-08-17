# P0-1: 修复 FILE_WRITE 路径穿越，写路径统一走 PrivateStore 防护

**Phase:** v0.9 P0
**Source:** SPEC §7.1、§12.2；OI-003 P0-1
**Priority:** high

## 目标
真实主路径中的文件写入不得逃逸 Agent 私有空间；`write` 工具与
`_phase_commit` 的 FILE_WRITE/FILE_PATCH 应用路径必须经过
`PrivateStore.resolve_path` 的 `..`/绝对路径/symlink 防护。

## 要求 / 规则
- `_phase_commit` 中 FILE_WRITE/FILE_PATCH 的 `target = home / path`
  替换为 `self._private_store.resolve_path(agent_id, path)`。
- `WritePrivateFileIntent` 与 `write` 工具在 Validate/Act 阶段增加
  路径静态检查：拒绝绝对路径、拒绝含 `..` 段的路径、拒绝空路径。
- 符号链接逃逸由 resolve_path 的 `resolve()` + containment 检查
  拦截（当前已有实现，需接到主路径）。
- 不允许把校验只放在 FileOps/PrivateStore 单元测试中；必须新增
  Simulation 主路径测试。

## 产出
- 修改后的提交路径（不得新增安全绕过）。
- 主路径回归测试：`write` 工具 + `_phase_commit` 对
  `../agent.b/...`、`/tmp/...`、symlink 逃逸均失败且不产生文件。

## 验收标准
- [ ] `sim._phase_commit` 写入 `../agent.b/workspace/pwned.txt` 不产生文件
- [ ] `write` 工具路径 `../agent.b/...` 返回失败（错误码 INVALID_ARGUMENT 或路径类错误码）
- [ ] 新增测试至少 3 条且穿过 Simulation 主路径（非仅 FileOps）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
