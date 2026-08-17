# Root Agent 的 delegate 工具定义不明确

**Severity:** Medium
**Source:** SPEC §4.2, §4.3

## 问题

SPEC 中提到 Root Agent 的 `delegate` "不是普通文件系统工具，而是系统提供的控制能力，用于生成并发送委派 E-mail"。但同时 Root Agent 的 tools 列表中包含 `delegate`，而子 Agent 的 tools 列表中也包含 `delegate`（§4.3）。

这导致歧义:

1. `delegate` 到底是一个统一的系统工具，还是每个 Agent 各自实现？
2. Root Agent 的 delegate 和子 Agent 的 delegate 行为是否一致？
3. 子 Agent 的 delegate 是否也等价于发送委派 E-mail？

## 建议

明确 `delegate` 工具的统一语义:

- 所有 Agent 的 `delegate` 都等价于发送委派 E-mail
- 区别仅在于权限范围（Root 可委派给所有直接子节点，子 Agent 只能委派给自己的直接子节点）
- 或者将 `delegate` 从工具列表中移除，改为系统隐式提供的能力

## 影响

影响 §4.1, §4.2, §4.3, §7.3 的实现。
