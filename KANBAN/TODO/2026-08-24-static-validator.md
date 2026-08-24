---
kind: task
phase: v0.11 扩展表面
source: 原 E7（缩）；SPEC §11.2/§15；三态收敛（2026-08-24）
priority: high
---

# 静态校验器：声明良构性（含边语义不变量与设备依赖）


## 目标
静态查"声明的良构性"，动态查"动作的合法性"，两者不混；校验器本身
**属闭包**（不可被配置包替换或绕过）。ProcessDef 废弃后去掉流程
可达性/终局检查（原 E7 缩化，2026-08-24 坍缩）。

## 要求 / 规则
- 三个时机：加载时（INSTALL_PACKAGE 全量静态，N9 联测）；运行时
  （复用 simulation.py PreValidate/CommitValidate，不重建）；生成时
  （root 自进化产物走同一套加载校验）；
- 五类基础检查：①引用完整性（gate/step/role 引用存在——按新模型：
  岗位/边/设备/能力引用存在）；②结构合法性（组织图无环、depends
  无环、domain 属 8 枚举、无死依赖）；③Authority 一致性（unresolved
  检测 + 四条不变量的静态判定部分）；④闭包边界（配置不得触碰
  tick/身份/Journal/授权内核/epoch/路径）；⑤版本兼容（新版本 vs
  运行中任务/决策的版本绑定）；
- 补四类（无流程检查）：⑥能力可达性（每任务/岗位能获得执行者/
  设备能力/审批人）；⑦权限单调性（delegated capability ⊆
  delegator；delegated authority ⊆ delegator；动态 delegate 时仍
  重查）；⑧敏感数据流（Customer 数据不进无权 Agent / Credential
  不进 prompt / PrivateStore 不跨 Deployment 泄漏 / cc 与广播不破
  信息权限）；⑨资源上限（并行分支/最大激活 Agent/最大 pending
  op/最大重试/最大循环/最大外部调用速率）；
- **边语义声明检查**：组织架构声明的边行为不违反四条治理不变量
  （N2 联测，SPEC §15）；
- **设备依赖检查**：设备依赖经接口声明，无跨设备直连（N1 联测）；
- 产物：通过 → INSTALL_PACKAGE 原子提交入 Journal；拒绝 →
  结构化错误列表 + **整体拒绝**（不半装）。

## 产出
- 静态校验规则清单（可执行）+ 内核模块（`validator.py`）；
- 作为 N1/N2/N9 的加载入口（设备/组织/包声明校验）。

## 验收标准
- [ ] 结构合法但永远无法运行的组织/任务声明被拒（能力不可达）
- [ ] root 生成引用未授权设备能力的岗位/任务声明被静态拒绝
      （不等运行时失败）
- [ ] 边语义声明违反四条治理不变量被拒
- [ ] 敏感数据流越界被拒（Customer 数据进无权 Agent、Credential
      进 prompt）
- [ ] 委派单调性静态 + 动态双重校验
- [ ] 校验器不可被配置包替换/绕过（有测试证明）
- [ ] 拒绝 = 结构化错误列表 + 整体拒绝（无半装）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
