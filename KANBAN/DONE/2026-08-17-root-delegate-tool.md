# Root Agent 的 delegate 工具定义不明确

**Severity:** Medium
**Source:** SPEC §4.2, §4.3
**Status:** RESOLVED
**Resolved:** 2026-08-17

## 问题

SPEC 中提到 Root Agent 的 `delegate` "不是普通文件系统工具，而是系统提供的控制能力，用于生成并发送委派 E-mail"。但同时 Root Agent 的 tools 列表中包含 `delegate`，而子 Agent 的 tools 列表中也包含 `delegate`（§4.3）。

## 决策：统一语义

**所有 Agent 的 `delegate` 都等价于发送委派 E-mail。**

具体规则：
1. `delegate` 是一个统一的系统工具，所有 Agent 共享相同语义
2. 执行 `delegate` = 生成并发送一封 `delegation` 类型的 E-mail
3. 区别仅在权限范围：
   - Root Agent 可委派给所有直接子节点
   - 子 Agent 只能委派给自己的直接子节点
   - 权限由 `AgentTree.can_delegate_to()` 控制
4. `delegate` 保留在工具列表中（不改为隐式能力），因为：
   - 需要显式声明才能进行权限检查
   - 与 `read`/`write`/`ls` 保持一致的工具模型
   - 方便审计日志记录

## 影响的 SPEC 章节

- §4.2: 删除 "不是普通文件系统工具" 的描述，改为统一工具语义
- §4.3: 确认子 Agent 的 delegate 行为与 Root 一致
- §7.3: 委派约束不变（只能向直接子节点委派）
