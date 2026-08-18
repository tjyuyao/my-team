---
kind: task
phase: v0.11 扩展协议
source: SPEC §11.4；用户定位补充（个体户/一人公司）
priority: high
---

# v0.11-17: Skill Package 系统（面向非专业用户的能力封装）


## 目标
非软件开发用户能够安装/卸载 Skill 包（SKILL.md + prompts + tools +
kb_seed + scripts + approval.json），由 ContextCompiler 按触发条件
把技能 SOP、模板与知识注入对应 Worker，全程不修改内核代码。

## 要求 / 规则
- Skill 包结构按 SPEC §11.4：
  `SKILL.md / prompts/ / tools/ / kb_seed/ / scripts/ / approval.json`。
- 安装即校验：SKILL.md 存在、工具 manifest 合法、审批策略合法、
  scripts 只能运行于 L0/L1 沙箱。
- ContextCompiler 集成触发注入：
  - 定义 `frontmatter` 触发条件（关键词/任务类型/角色）；
  - 命中后按 token budget 注入 SOP、模板、KB 条目。
- Skill 与场景包可组合：场景包可引用多个 Skill。
- 权限边界：Skill 注入的 KB 与工具仍受 PermissionEngine 与
  OperationPolicy 约束。

## 产出
- Skill 包加载器与校验器。
- 首个示例 Skill（建议：外贸询盘分级 或 小红书商品笔记）。
- ContextCompiler 触发注入测试。

## 验收标准
- [ ] 非开发者通过目录安装 Skill 包，内核代码零改动
- [ ] 非法 Skill 包被拒绝并给出结构化错误
- [ ] 命中触发条件时，Worker briefing 包含该 Skill 的 SOP 与知识
- [ ] Skill 内工具受 OperationPolicy 默认拒绝约束
- [ ] 新增测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
