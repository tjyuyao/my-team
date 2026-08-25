---
kind: task
status: rejected
phase: v0.11 场景
source: SPEC §11.4（2026-08-24 重划版）；v0.11-plan 待重划项；依赖 E5/E7
priority: medium
---

# Skill Package 系统（面向非专业用户的能力封装）

> **否决（2026-08-25）**：裁撤。与 grill 讨论后的设计差距太大，独立
> Skill 包系统没有用。有效部分已并入 N4 记忆系统——skill 就是 agent
> 私有记忆的种子（§4.2），「晋升 = 发布为组织资产」（§4.2），来源段
> 隔离（§8.4）。不再单独做 Skill 包加载器/校验入口。


## 目标
非软件开发用户能够安装/卸载 Skill 包（SOP + 提示词模板 + 知识 +
工具引用），由 ContextCompiler 按触发条件把技能 SOP、模板与知识以
带来源标签的方式注入对应 Worker，全程不修改内核代码。旧设计
（`scripts/` 自带脚本、`approval.json`、关键词命中直注正文）已废弃，
见 SPEC §11.4 重划注记。

## 要求 / 规则
- 结构按 SPEC §11.4（2026-08-24 版）：skill.manifest / SKILL.md /
  prompts/ / kb_seed/；`scripts/` 与 `approval.json` 默认不存在——
  脚本是可执行能力，默认独立成能力包，Skill 只引用不携带（结构
  约定而非安全强制；内嵌须如实申报并同等对待，见 E5 审计制边界）；
  审批由 OperationPolicy + 编排层 gate 决定，Skill 无权声明审批策略。
- SKILL.md 正文按结构化来源段组织：`[SKILL_INSTRUCTION]` /
  `[POLICY]` / `[UNTRUSTED_CUSTOMER_CONTENT]`。ContextCompiler 注入时
  保留来源标签并规定优先级：POLICY 来自内核与 Owner，不可被
  SKILL_INSTRUCTION 覆盖；客户内容永不作为系统指令。
- 触发注入：frontmatter 触发条件（关键词/任务类型/角色）命中后在
  token budget 内注入，注入内容必须带来源与信任等级。
- 安装即校验：manifest 合法、capabilities_requested 仅引用已有 Tool、
  来源段齐备；非法包整体拒绝（复用 E5 包边界 + E7 校验器）。
- 权限边界：Skill 注入的 KB 与引用的工具仍受 PermissionEngine 与
  OperationPolicy deny-by-default 约束；Skill 不能自行提升权限。
- **agent 隐私（2026-08-24 三态收敛）**：skill 完全属于 agent 态
  ——skill 包安装 = 注入 agent 私密记忆的**种子**（org 提供种子，
  agent 持有私有副本并可进化，N4 记忆系统承载；晋升 = 从 agent 态
  发布为组织资产/设备能力）。
- 首个示例 Skill 属场景资产：待最小测试向量闭合后交付。

## 产出
- Skill 包加载器与校验入口（复用 E5 包 schema + E7 validator）。
- ContextCompiler 结构化来源段注入实现与测试。
- 示例 Skill 一个（建议：外贸询盘分级 或 小红书商品笔记；
  测试向量闭合后）。

## 验收标准
- [ ] 非开发者通过目录安装 Skill 包，内核代码零改动
- [ ] 携带未申报可执行条目（如 scripts 未在 manifest 申报执行器分级）
      的包被整体拒绝并给出结构化错误；正确申报的内嵌脚本按其分级
      进沙箱
- [ ] 命中触发条件时 Worker briefing 含该 Skill 的 SOP/知识，且各段
      带来源标签；[UNTRUSTED_*] 内容不进入指令位
- [ ] POLICY 段不被 SKILL_INSTRUCTION 覆盖（有测试证明）
- [ ] Skill 引用的工具未被 allowlist 时调用被 POLICY_DENIED 拒绝
- [ ] 新增测试；`uv run pytest -q` 全绿；ruff/mypy 通过
