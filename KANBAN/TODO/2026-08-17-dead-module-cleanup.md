# v0.9-14: 僵尸组件清理与接线（IdentityEnforcer/Executors/FileOps/DelegationProtocol/HumanControl）

**Phase:** v0.9 收口
**Source:** SPEC §2、§12；OI-003 P1-5
**Priority:** medium

## 目标
消除"测试中存活、主路径未接线"的模块，让安全与执行模式层真实
生效，而不是看起来完整。

## 要求 / 规则
- `FileOps`：主路径文件读写统一经 FileOps/PrivateStore；删除
  simulation 内联的重复逻辑。
- `IdentityEnforcer`：接入 ToolContext 创建路径，或明确删除；
  `validate_file_access` 不得为空实现。
- `executors.py`（DiscreteAsync/BoundedMicroLoop）：若 v0.9 启用
  micro-loop 则接入 Runtime；否则在模块 docstring 标注
  "not wired yet; target v0.10" 并删除测试中的误导性用法。
- `DelegationProtocol`：委派逻辑收敛到该模块，或删除该模块。
- `HumanControl`：接入 Runtime 循环（apply_pending_duration_changes
  在每 tick 边界调用）。

## 产出
- 每个模块有明确状态：主路径使用 / 外部 API / 删除。
- 主路径不再存在绕过 FileOps 的文件写。

## 验收标准
- [ ] `grep` 可证明 simulation 不再内联绕过 FileOps 的写路径
- [ ] IdentityEnforcer 被实例化并参与 ToolContext 创建，或已删除
- [ ] HumanControl 的 tick duration 变更真实生效
- [ ] 所有模块 docstring 标注接线状态
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
