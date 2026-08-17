# 谓词三级边界（L0 声明式 / L1 受限纯函数 / L2 外部能力）

**Kind:** task
**Phase:** v0.11 扩展表面
**Source:** 审阅 P0-8、§四.2；OPEN_ISSUE 能力层
**Priority:** high

## 目标
谓词只能解释状态，不能改变状态，也不能偷偷调用外部世界。当前
`python_compute` / `python_transform` 是 LOCAL_PROCESS（任意代码），
若直接用作 transition `when` / gate 判定会破坏确定性重放、静态分析、
权限边界、沙箱与可审计性。本任务把谓词能力分三级并强制降级。

## 要求 / 规则
- **L0 纯声明式**（只允许有限 DSL）：
  `field == literal` / `field in set` / `all(...)` / `any(...)` /
  `exists(...)` / `count(...) < n` / `and` / `or` / `not`。
- **L1 受限纯函数**：无网络、无文件写、无随机、无直接读当前时间、
  固定 I/O schema、固定超时与资源上限、结果可缓存可重放。
- **L2 外部能力**：必须建模为 Tool / PendingOperation，**不能伪装成
  predicate**。
- 原则：谓词只解释状态；任何改变状态或调用外部世界的需求必须走 L2
  （进入工具/op 路径），不能下沉到 predicate。

## 产出
- predicate DSL grammar + 三级边界 spec。
- L0 解释器 + 校验器（拒绝越级：detect 网络/文件写/时间读取/随机）。
- transition `when` 与 gate 判定只接受 L0/L1。

## 验收标准
- [ ] 任意 predicate 纯、有限、可审计、可重放
- [ ] transition `when` 无法逃逸到网络/文件写/直接读当前时间/随机
- [ ] L0 表达式确定性重放（同输入同输出，无副作用）
- [ ] 越级谓词（L2 伪装）被静态校验拒绝并提示改为 Tool/op
- [ ] 最小测试向量段：transition `when` 用 L0 谓词驱动流程 通过
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
