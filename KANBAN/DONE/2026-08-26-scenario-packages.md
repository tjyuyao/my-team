---
kind: task
status: rejected
phase: v0.11 post-agent
source: SPEC §11（2026-08-24 重划 + 定位修订版）；v0.11-plan 待重划项；依赖 E1/E4/E5/E7
priority: medium
---

# 场景包系统（完全扩展能力的自包含单元）与场景 demo
> **废弃（2026-08-25，Owner 定）**：v0.11 计划整体归档（见
> `KANBAN/PLAN/2026-08-25-v0.11.0-plan.archived.md`），本卡随计划
> 一并废弃。原因：重构方案重议——原「增量功能 + 事后结构重排」
> 路线不满足三态质量前提（测试绿 ≠ 结构正确）。本卡内容留档备查，
> 不执行；新方案见 `docs/THREE_STATE_REFACTOR_PLAN.md`。



## 目标
五个目标场景均以「场景包」安装运行。**三态收敛后（2026-08-24）：
场景包 = 一个 org 的定义——core 配置 + devices（含组织架构设备
数据：positions/边语义/授权）+ agents（初始配置与记忆种子）**；
自定义组织架构与协作设备合法。场景包是**具备完全扩展能力的自包含
安装单元**（2026-08-24 Maintainer 决策）：除声明配置与数据外，可自带
可执行能力——包括以 ToolPlugin（§6.2）定义新工具、ingress
adapter、受控脚本；也可引用既有能力包，但引用仅是复用手段，不是
定位限制。经 `INSTALL_PACKAGE` 事务安装并过校验；内核代码不因场景
变化而修改。`approval_policies.json` 被三查分离取代、`scenario.json`
并入 package.yaml；信任模型为审计制（E5/N9 决策记录）。

## 要求 / 规则
- 结构按 SPEC §11.1（2026-08-24 版）：package.yaml / process/ /
  org/ / authority/ / tools/ / record_schemas/ / ingress_mappings/ /
  schedules.json / kb_seed/ / kpi/。
- 能力获取两路平权：① 自带——tools/ 下以 ToolPlugin 定义新工具、
  受控脚本等，须在 manifest 如实申报（capabilities_requested +
  执行器分级）；② 引用既有能力包，以稳定全限定 ID
  （`package_id:entity_type:entity_id@version`，E4）。未申报的
  可执行条目 → 加载期整体拒绝（审计制边界，见 E5）。
- 裁决权配置走 authority/*.yaml（8 域 AuthorityGrant）；审批由编排层
  gate（authority_ref）+ OperationPolicy 三查分离承接；无独立审批
  策略文件。
- 安装路径复用 E4 多阶段状态机（Activate 为唯一运行时可见切换点）
  + E7 静态校验器（声明类条目）；拒绝 = 结构化错误列表 +
  整体拒绝（不半装）。
- kb_seed 内容默认不可信（C 类数据），注入须经来源标签隔离（§11.4）。
- demo 纪律：先交付「可加载、可通过校验」的场景包配置骨架；
  端到端 demo 待最小测试向量闭合后交付（v0.11 门约束）。

## 产出
- 场景包加载器：INSTALL_PACKAGE 接线 + tools/ 的 ToolPlugin 注册
  路径（manifest 申报驱动）+ 校验入口（复用 E7 validator）。
- 软件公司场景包骨架（加载校验通过，含至少一个自带 ToolPlugin 工具）。
- 其余四个场景包（小说工作室/电商/自媒体/知识星球）配置骨架与验收清单。

## 验收标准
- [ ] 从配置安装场景包（含自带工具），内核代码零改动
- [ ] 自带 ToolPlugin 工具安装后出现在 ToolRegistry，未被 allowlist
      时调用被 POLICY_DENIED 拒绝
- [ ] 未如实申报的可执行条目（缺 capabilities_requested/执行器分级）
      加载即整体拒绝并给出结构化错误
- [ ] 自带能力与引用能力运行时地位相同（有测试）
- [ ] 引用不存在工具的 ProcessDef 在加载期被静态拒绝
      （不等运行时失败）
- [ ] INSTALL_PACKAGE 入 Journal 可审计可回滚；Activate 前运行时不可见
- [ ] 最小测试向量闭合后：软件公司场景包端到端 demo 可运行
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
