---
kind: task
status: completed
phase: v0.11 扩展表面
source: 原 E6；SPEC §5.3/§6.3；三态收敛（2026-08-24）
priority: high
---

# 谓词分级：L0 声明式 / L1 受限纯函数 / L2 外部能力


## 目标
谓词只能解释状态，不能改变状态，也不能偷偷调用外部世界。把谓词
能力分三级并强制降级（L1 受限解释器与工具组合环境**同源**——
同一边界、同一沙箱，SPEC §6.3）。

## 要求 / 规则
- **L0 纯声明式**（有限 DSL）：`field == literal` / `field in set` /
  `all(...)` / `any(...)` / `exists(...)` / `count(...) < n` /
  `and` / `or` / `not`；
- **L1 受限纯函数**：无网络、无文件写、无随机、无直接读当前时间、
  固定 I/O schema、固定超时与资源上限、结果可缓存可重放；
- **L2 外部能力**：必须建模为 Tool/PendingOperation（设备能力），
  **不能伪装成 predicate**；
- 原则：谓词只解释状态；任何改变状态或调用外部世界的需求必须走
  L2（进入工具/op 路径），不能下沉到 predicate；
- **bash 明确 v0.13**（2026-08-24 版本切分）：受限执行器族
  （python 模组 + bash 脚本）在 v0.13 扩展；本卡只做 python 受限
  解释器边界与 L0 解释器；
- 受限解释器/沙箱 = 内核执行真理（三态收敛），非设备数据。

## 产出
- predicate DSL grammar + 三级边界 spec；
- L0 解释器 + 校验器（拒绝越级：detect 网络/文件写/时间读取/随机）；
- L1 受限执行器（python 模组）与工具组合环境共用边界（N4 联测：
  私有工具条目 type=tool 的 python 模组在此执行）。

## 验收标准
- [x] 任意 predicate 纯、有限、可审计、可重放
- [x] transition `when`/gate 判定无法逃逸到网络/文件写/直接读当前
      时间/随机
- [x] L0 表达式确定性重放（同输入同输出，无副作用）
- [x] 越级谓词（L2 伪装）被静态校验拒绝并提示改为 Tool/op
- [x] 最小测试向量段：transition `when` 用 L0 谓词驱动流程 通过
- [x] `uv run pytest -q` 全绿；ruff/mypy 通过

## 完成注记（2026-08-24）

- 交付：`predicate.py`（三级边界 spec + L0 DSL + L1 静态校验 + L2 拒绝
  路径）+ `python_worker.py` L1 对齐（import 门显式禁 random/time）+
  68 测试；
- 关键决策：L0 结构性纯（AST 只遍历不执行）；L1 与工具组合环境同源
  （复用 compute 受限解释器，谓词白名单剔除 datetime/uuid）；L2 只做
  拒绝路径；bash 未做（v0.13）；
- 遗留：L1 静态校验保守近似（误报偏好拒绝，运行时 import 门兜底）；
  L1 无 OS 级隔离（与 python_compute 一致的诚实分类）。
