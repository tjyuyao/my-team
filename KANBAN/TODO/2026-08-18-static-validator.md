# 静态校验器：五类基础检查 + 可达性/终局/权限单调/敏感数据流/资源上限

**Kind:** task
**Phase:** v0.11 扩展表面
**Source:** 审阅 §八；OPEN_ISSUE 静态校验器节；SPEC §11.2
**Priority:** high

## 目标
把 OPEN_ISSUE 已定的"静态校验器职责边界"落成可执行检查规则清单。
静态查"声明的良构性"，动态查"动作的合法性"，两者不混。校验器本身
**属闭包**：不可被配置包替换或绕过。

## 要求 / 规则
- 三个时机：
  1. 加载时（INSTALL_PACKAGE 全量静态）；
  2. 运行时（复用 `simulation.py` PreValidate/CommitValidate，不重建）；
  3. 生成时（root 自进化产物走同一套加载校验）。
- 五类基础检查：
  1. 引用完整性（gate/step 引用的 role、step、check、escalation 存在）；
  2. 结构合法性（组织树无环、depends 无环、domain 属 8 枚举、无死依赖）；
  3. Authority 一致性（unresolved 检测 + 四条不变量的可静态判定部分）；
  4. 闭包边界（配置不得触碰 tick/身份/Journal/授权内核/epoch/路径）；
  5. 版本兼容（新版本 vs 运行中 ProcessInstance 版本绑定）。
- 补五类：
  6. 能力可达性（每 Step 能获得执行者/工具/权限/record schema/
     Integration/审批人）；
  7. 终局可达性（无不可达 step / 成功终局 / 失败终局 / 无 escalation
     永久阻塞 / 无界循环 / 无 deadline 等待）；
  8. 权限单调性（delegated capability ⊆ delegator；delegated
     authority ⊆ delegator；动态 delegate 时仍重查）；
  9. 敏感数据流（Customer 数据不进无权 Agent / Credential 不进 prompt /
     PrivateStore 不跨 Deployment 泄漏 / cc 与广播不破信息权限）；
  10. 资源上限（并行分支 / 最大激活 Agent / 最大 pending op / 最大重试 /
      最大循环 / 最大外部调用速率）。
- 产物：通过 → INSTALL_PACKAGE 原子提交入 Journal；拒绝 → 结构化错误
  列表 + **整体拒绝**（不半装）。

## 产出
- 静态校验规则清单（可执行，非概念）+ 内核模块（`validator.py`）。
- 与 SPEC §11.2 对齐；作为 package-trust-boundary / predicate-dsl /
  process-model 的加载入口。

## 验收标准
- [ ] 结构合法但永远无法运行的流程被拒（能力不可达 / 终局不可达）
- [ ] root 生成引用未授权工具的 ProcessDef 被静态拒绝（不等运行时失败）
- [ ] 无 escalation 的永久阻塞被拒
- [ ] 敏感数据流越界被拒（Customer 数据进无权 Agent、Credential 进 prompt）
- [ ] 委派单调性静态 + 动态双重校验
- [ ] 校验器不可被配置包替换/绕过（有测试证明）
- [ ] 拒绝 = 结构化错误列表 + 整体拒绝（无半装）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
